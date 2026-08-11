import hashlib
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from io import BytesIO
from itertools import zip_longest

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
from werkzeug.security import check_password_hash, generate_password_hash


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONEY = Decimal("0.01")
RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
ACCESS_ALPHABET = string.ascii_uppercase + string.digits


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    access_code_hash: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[str] = mapped_column(String(10))
    business_name: Mapped[str] = mapped_column(String(160))
    business_email: Mapped[str] = mapped_column(String(254), default="")
    business_phone: Mapped[str] = mapped_column(String(80), default="")
    client_name: Mapped[str] = mapped_column(String(160))
    client_email: Mapped[str] = mapped_column(String(254), default="")
    client_address: Mapped[str] = mapped_column(Text, default="")
    invoice_number: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str] = mapped_column(String(4))
    vat_percentage: Mapped[str] = mapped_column(String(16), default="0")
    bank_name: Mapped[str] = mapped_column(String(160), default="")
    account_number: Mapped[str] = mapped_column(String(160), default="")
    line_items: Mapped[list] = mapped_column(JSON, default=list)
    materials: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
        DATABASE_URL=normalize_database_url(
            os.environ.get("DATABASE_URL", "sqlite:///invoice_generator.sqlite3")
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=512 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
    )
    if test_config:
        app.config.update(test_config)

    engine_options = {"pool_pre_ping": True}
    if app.config["DATABASE_URL"] == "sqlite://":
        engine_options.update(
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    engine = create_engine(app.config["DATABASE_URL"], **engine_options)
    database = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
    Base.metadata.create_all(engine)
    app.extensions["database"] = database
    app.extensions["engine"] = engine

    @app.teardown_appcontext
    def remove_database_session(_exception=None):
        database.remove()

    @app.context_processor
    def template_helpers():
        return {"csrf_token": get_csrf_token}

    @app.before_request
    def verify_csrf():
        if request.method == "POST":
            expected = session.get("_csrf_token", "")
            supplied = request.form.get("_csrf", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                abort(400, "This form expired. Please go back, refresh the page, and try again.")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'",
        )
        return response

    register_routes(app, database)
    return app


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        session["_csrf_token"] = token
    return token


def canonical_record_id(value: str) -> str:
    return (value or "").strip().lower()


def generate_access_code() -> str:
    raw = "".join(secrets.choice(ACCESS_ALPHABET) for _ in range(20))
    return "-".join(raw[index : index + 5] for index in range(0, len(raw), 5))


def parse_decimal(value, label, errors, *, minimum=Decimal("0"), maximum=None):
    try:
        number = Decimal((value or "").strip())
        if not number.is_finite():
            raise InvalidOperation
        rounded = number.quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError):
        errors.append(f"{label} must be a valid number.")
        return Decimal("0")
    if number < minimum:
        errors.append(f"{label} cannot be less than {minimum}.")
    if maximum is not None and number > maximum:
        errors.append(f"{label} cannot be greater than {maximum}.")
    return rounded


def collect_invoice_form(form, *, fixed_record_id=None):
    errors = []
    record_id = canonical_record_id(fixed_record_id or form.get("record_id"))
    if not RECORD_ID_PATTERN.fullmatch(record_id):
        errors.append(
            "Record ID must be 3–40 characters and contain only letters, numbers, hyphens, or underscores."
        )

    document_type = form.get("document_type", "Invoice")
    if document_type not in {"Invoice", "Quote"}:
        errors.append("Document type must be Invoice or Quote.")

    currency = form.get("currency", "R")
    if currency not in {"R", "$", "€", "£"}:
        errors.append("Choose a supported currency.")

    required = {
        "business_name": "Business name",
        "client_name": "Client name",
        "invoice_number": "Invoice or quote number",
        "issue_date": "Issue date",
    }
    for field, label in required.items():
        if not (form.get(field) or "").strip():
            errors.append(f"{label} is required.")

    issue_date = (form.get("issue_date") or "").strip()[:10]
    try:
        datetime.strptime(issue_date, "%Y-%m-%d")
    except ValueError:
        errors.append("Issue date must be a valid date.")

    vat = parse_decimal(
        form.get("vat_percentage", "0"),
        "VAT percentage",
        errors,
        maximum=Decimal("100"),
    )

    line_items = []
    descriptions = form.getlist("description")
    rates = form.getlist("rate")
    quantities = form.getlist("quantity")
    for index, (description, rate, quantity) in enumerate(
        zip_longest(descriptions[:101], rates[:101], quantities[:101], fillvalue=""),
        start=1,
    ):
        description, rate, quantity = description.strip(), rate.strip(), quantity.strip()
        if not any((description, rate, quantity)):
            continue
        if not all((description, rate, quantity)):
            errors.append(f"Labour item {index} is incomplete.")
            continue
        parsed_rate = parse_decimal(rate, f"Labour item {index} rate", errors)
        parsed_quantity = parse_decimal(quantity, f"Labour item {index} hours", errors)
        line_items.append(
            {
                "description": description[:500],
                "rate": str(parsed_rate),
                "quantity": str(parsed_quantity),
            }
        )
    if not line_items:
        errors.append("Add at least one complete labour line item.")
    if len(line_items) > 100:
        errors.append("An invoice can contain at most 100 labour line items.")

    materials = []
    material_descriptions = form.getlist("material_description")
    material_costs = form.getlist("material_cost")
    for index, (description, cost) in enumerate(
        zip_longest(material_descriptions[:101], material_costs[:101], fillvalue=""),
        start=1,
    ):
        description, cost = description.strip(), cost.strip()
        if not any((description, cost)):
            continue
        if not all((description, cost)):
            errors.append(f"Material item {index} is incomplete.")
            continue
        parsed_cost = parse_decimal(cost, f"Material item {index} cost", errors)
        materials.append({"description": description[:500], "cost": str(parsed_cost)})
    if len(materials) > 100:
        errors.append("An invoice can contain at most 100 material items.")

    values = {
        "record_id": record_id,
        "document_type": document_type,
        "business_name": (form.get("business_name") or "").strip()[:160],
        "business_email": (form.get("business_email") or "").strip()[:254],
        "business_phone": (form.get("business_phone") or "").strip()[:80],
        "client_name": (form.get("client_name") or "").strip()[:160],
        "client_email": (form.get("client_email") or "").strip()[:254],
        "client_address": (form.get("client_address") or "").strip()[:1000],
        "invoice_number": (form.get("invoice_number") or "").strip()[:100],
        "issue_date": issue_date,
        "currency": currency,
        "vat_percentage": str(vat),
        "bank_name": (form.get("bank_name") or "").strip()[:160],
        "account_number": (form.get("account_number") or "").strip()[:160],
        "line_items": line_items,
        "materials": materials,
    }
    return values, errors


def invoice_values(invoice):
    return {
        "record_id": invoice.record_id,
        "document_type": invoice.document_type,
        "business_name": invoice.business_name,
        "business_email": invoice.business_email,
        "business_phone": invoice.business_phone,
        "client_name": invoice.client_name,
        "client_email": invoice.client_email,
        "client_address": invoice.client_address,
        "invoice_number": invoice.invoice_number,
        "issue_date": invoice.issue_date,
        "currency": invoice.currency,
        "vat_percentage": invoice.vat_percentage,
        "bank_name": invoice.bank_name,
        "account_number": invoice.account_number,
        "line_items": invoice.line_items or [],
        "materials": invoice.materials or [],
    }


def apply_values(invoice, values):
    for field in (
        "document_type",
        "business_name",
        "business_email",
        "business_phone",
        "client_name",
        "client_email",
        "client_address",
        "invoice_number",
        "issue_date",
        "currency",
        "vat_percentage",
        "bank_name",
        "account_number",
        "line_items",
        "materials",
    ):
        setattr(invoice, field, values[field])


def calculate_invoice(invoice):
    labour = []
    subtotal = Decimal("0")
    for item in invoice.line_items or []:
        rate = Decimal(item["rate"])
        quantity = Decimal(item["quantity"])
        total = (rate * quantity).quantize(MONEY, rounding=ROUND_HALF_UP)
        subtotal += total
        labour.append({**item, "rate": rate, "quantity": quantity, "total": total})

    materials = []
    materials_total = Decimal("0")
    for item in invoice.materials or []:
        cost = Decimal(item["cost"])
        materials_total += cost
        materials.append({**item, "cost": cost})

    vat_percentage = Decimal(invoice.vat_percentage)
    taxable_total = subtotal + materials_total
    vat_on_line_items = (subtotal * vat_percentage / Decimal("100")).quantize(
        MONEY, rounding=ROUND_HALF_UP
    )
    vat_on_materials = (materials_total * vat_percentage / Decimal("100")).quantize(
        MONEY, rounding=ROUND_HALF_UP
    )
    vat = (taxable_total * vat_percentage / Decimal("100")).quantize(
        MONEY, rounding=ROUND_HALF_UP
    )
    return {
        "line_items": labour,
        "materials": materials,
        "subtotal": subtotal.quantize(MONEY),
        "materials_total": materials_total.quantize(MONEY),
        "vat_percentage": vat_percentage,
        "vat_on_line_items": vat_on_line_items,
        "vat_on_materials": vat_on_materials,
        "vat": vat,
        "total": (taxable_total + vat).quantize(MONEY),
    }


def is_authorized(record_id):
    return record_id in session.get("authorized_records", [])


def authorize(record_id):
    records = set(session.get("authorized_records", []))
    records.add(record_id)
    session["authorized_records"] = sorted(records)[-50:]
    session.permanent = True


def require_invoice_access(view):
    @wraps(view)
    def wrapped(record_id, *args, **kwargs):
        record_id = canonical_record_id(record_id)
        if not is_authorized(record_id):
            flash("Enter the record ID and private access code to continue.", "warning")
            return redirect(url_for("home", record_id=record_id))
        return view(record_id, *args, **kwargs)

    return wrapped


def register_routes(app, database):
    def find_invoice(record_id):
        return database.scalar(select(Invoice).where(Invoice.record_id == record_id))

    @app.get("/health")
    def health():
        database.execute(select(1))
        return {"status": "ok"}

    @app.get("/")
    def home():
        return render_template("home.html", suggested_record_id=request.args.get("record_id", ""))

    @app.get("/new")
    def new_invoice():
        values = {
            "record_id": "",
            "document_type": "Invoice",
            "business_name": "",
            "business_email": "",
            "business_phone": "",
            "client_name": "",
            "client_email": "",
            "client_address": "",
            "invoice_number": "",
            "issue_date": datetime.now().date().isoformat(),
            "currency": "R",
            "vat_percentage": "15.00",
            "bank_name": "",
            "account_number": "",
            "line_items": [{"description": "", "rate": "", "quantity": ""}],
            "materials": [],
        }
        return render_template("editor.html", values=values, errors=[], editing=False)

    @app.post("/invoices")
    def create_invoice():
        values, errors = collect_invoice_form(request.form)
        if find_invoice(values["record_id"]):
            errors.append("That record ID is already in use. Choose another ID or open the existing record.")
        if errors:
            return render_template("editor.html", values=values, errors=errors, editing=False), 400

        access_code = generate_access_code()
        invoice = Invoice(
            record_id=values["record_id"],
            access_code_hash=generate_password_hash(access_code),
            document_type=values["document_type"],
            business_name=values["business_name"],
            client_name=values["client_name"],
            invoice_number=values["invoice_number"],
            issue_date=values["issue_date"],
            currency=values["currency"],
        )
        apply_values(invoice, values)
        database.add(invoice)
        try:
            database.commit()
        except IntegrityError:
            database.rollback()
            errors.append("That record ID was just claimed. Choose another one.")
            return render_template("editor.html", values=values, errors=errors, editing=False), 409

        authorize(invoice.record_id)
        session["new_access_code"] = {"record_id": invoice.record_id, "code": access_code}
        logger.info("Created invoice record %s", invoice.record_id)
        return redirect(url_for("saved_invoice", record_id=invoice.record_id))

    @app.post("/open")
    def open_invoice():
        record_id = canonical_record_id(request.form.get("record_id"))
        access_code = (request.form.get("access_code") or "").strip().upper()
        invoice = find_invoice(record_id)
        if not invoice or not check_password_hash(invoice.access_code_hash, access_code):
            flash("Record ID or access code is incorrect.", "error")
            return redirect(url_for("home", record_id=record_id))
        authorize(record_id)
        return redirect(url_for("edit_invoice", record_id=record_id))

    @app.get("/invoices/<record_id>/edit")
    @require_invoice_access
    def edit_invoice(record_id):
        invoice = find_invoice(record_id)
        if not invoice:
            abort(404)
        return render_template(
            "editor.html", values=invoice_values(invoice), errors=[], editing=True
        )

    @app.post("/invoices/<record_id>")
    @require_invoice_access
    def update_invoice(record_id):
        invoice = find_invoice(record_id)
        if not invoice:
            abort(404)
        values, errors = collect_invoice_form(request.form, fixed_record_id=record_id)
        if errors:
            return render_template("editor.html", values=values, errors=errors, editing=True), 400
        apply_values(invoice, values)
        database.commit()
        logger.info("Updated invoice record %s", record_id)
        flash("Your changes were saved.", "success")
        return redirect(url_for("saved_invoice", record_id=record_id))

    @app.get("/invoices/<record_id>/saved")
    @require_invoice_access
    def saved_invoice(record_id):
        invoice = find_invoice(record_id)
        if not invoice:
            abort(404)
        access_code = None
        one_time = session.pop("new_access_code", None)
        if one_time and one_time.get("record_id") == record_id:
            access_code = one_time.get("code")
        return render_template("saved.html", invoice=invoice, access_code=access_code)

    @app.get("/invoices/<record_id>/pdf")
    @require_invoice_access
    def invoice_pdf(record_id):
        invoice = find_invoice(record_id)
        if not invoice:
            abort(404)
        from weasyprint import HTML

        totals = calculate_invoice(invoice)
        html = render_template("invoice_pdf.html", invoice=invoice, totals=totals)
        pdf = HTML(string=html, base_url=request.url_root).write_pdf()
        kind = invoice.document_type.lower()
        return send_file(
            BytesIO(pdf),
            as_attachment=True,
            download_name=f"{record_id}-{kind}.pdf",
            mimetype="application/pdf",
        )

    @app.post("/invoices/<record_id>/lock")
    @require_invoice_access
    def lock_invoice(record_id):
        records = set(session.get("authorized_records", []))
        records.discard(record_id)
        session["authorized_records"] = sorted(records)
        flash("This invoice has been locked on this browser.", "success")
        return redirect(url_for("home", record_id=record_id))

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def friendly_error(error):
        return render_template("error.html", error=error), getattr(error, "code", 500)


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
