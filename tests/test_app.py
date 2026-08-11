from decimal import Decimal
import re

import pytest

from app import Invoice, calculate_invoice, create_app


@pytest.fixture()
def app():
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE_URL": "sqlite://",
            "SESSION_COOKIE_SECURE": False,
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client):
    client.get("/")
    with client.session_transaction() as current_session:
        return current_session["_csrf_token"]


def valid_invoice(token, **overrides):
    data = {
        "_csrf": token,
        "record_id": "acme-august-2026",
        "document_type": "Invoice",
        "business_name": "Acme Services",
        "business_email": "accounts@example.com",
        "business_phone": "0123456789",
        "client_name": "Example Client",
        "client_email": "client@example.com",
        "client_address": "1 Main Road",
        "invoice_number": "INV-42",
        "issue_date": "2026-08-11",
        "currency": "R",
        "vat_percentage": "15",
        "bank_name": "Example Bank",
        "account_number": "123456789",
        "description": ["Consulting"],
        "rate": ["500.00"],
        "quantity": ["2.5"],
        "material_description": ["Cable"],
        "material_cost": ["100.00"],
    }
    data.update(overrides)
    return data


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_create_invoice_generates_one_time_access_code(app, client):
    response = client.post("/invoices", data=valid_invoice(csrf(client)), follow_redirects=True)
    assert response.status_code == 200
    assert b"Save this private access code now" in response.data

    database = app.extensions["database"]
    invoice = database.query(Invoice).filter_by(record_id="acme-august-2026").one()
    assert invoice.business_name == "Acme Services"
    assert invoice.access_code_hash != ""


def test_duplicate_record_id_is_rejected(client):
    first = valid_invoice(csrf(client))
    assert client.post("/invoices", data=first).status_code == 302
    second = valid_invoice(csrf(client))
    response = client.post("/invoices", data=second)
    assert response.status_code == 400
    assert b"already in use" in response.data


def test_private_access_code_reopens_and_updates_invoice(app, client):
    created = client.post(
        "/invoices", data=valid_invoice(csrf(client)), follow_redirects=True
    )
    match = re.search(rb'<code id="access-code">([^<]+)</code>', created.data)
    assert match
    access_code = match.group(1).decode()

    returning_client = app.test_client()
    opened = returning_client.post(
        "/open",
        data={
            "_csrf": csrf(returning_client),
            "record_id": "ACME-August-2026",
            "access_code": access_code.lower(),
        },
        follow_redirects=True,
    )
    assert opened.status_code == 200
    assert b"Edit your invoice" in opened.data

    updated = returning_client.post(
        "/invoices/acme-august-2026",
        data=valid_invoice(
            csrf(returning_client),
            record_id="an-attempted-change",
            business_name="Acme Services Updated",
        ),
        follow_redirects=True,
    )
    assert updated.status_code == 200
    database = app.extensions["database"]
    invoice = database.query(Invoice).filter_by(record_id="acme-august-2026").one()
    assert invoice.business_name == "Acme Services Updated"


def test_wrong_access_code_does_not_open_invoice(client):
    client.post("/invoices", data=valid_invoice(csrf(client)))
    client.post(
        "/invoices/acme-august-2026/lock",
        data={"_csrf": csrf(client)},
    )
    response = client.post(
        "/open",
        data={
            "_csrf": csrf(client),
            "record_id": "acme-august-2026",
            "access_code": "WRONG-WRONG-WRONG-WRONG",
        },
        follow_redirects=True,
    )
    assert b"Record ID or access code is incorrect" in response.data


def test_pdf_generation(client):
    client.post("/invoices", data=valid_invoice(csrf(client)))
    response = client.get("/invoices/acme-august-2026/pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF")


def test_invalid_negative_values_are_rejected(client):
    response = client.post(
        "/invoices",
        data=valid_invoice(csrf(client), rate=["-1"]),
    )
    assert response.status_code == 400
    assert b"cannot be less than" in response.data


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999999"])
def test_non_finite_or_extreme_values_are_rejected(client, value):
    response = client.post(
        "/invoices",
        data=valid_invoice(csrf(client), rate=[value]),
    )
    assert response.status_code == 400
    assert b"must be a valid number" in response.data


def test_malformed_line_item_columns_are_rejected(client):
    response = client.post(
        "/invoices",
        data=valid_invoice(
            csrf(client),
            description=["Consulting", "Extra work"],
            rate=["500.00"],
            quantity=["2.5", "1"],
        ),
    )
    assert response.status_code == 400
    assert b"Labour item 2 is incomplete" in response.data


def test_invalid_date_is_rejected(client):
    response = client.post(
        "/invoices",
        data=valid_invoice(csrf(client), issue_date="2026-02-30"),
    )
    assert response.status_code == 400
    assert b"Issue date must be a valid date" in response.data


def test_edit_requires_authorization(client):
    response = client.get("/invoices/private-record/edit")
    assert response.status_code == 302
    assert "/?record_id=private-record" in response.location


def test_totals_use_decimal_math():
    invoice = Invoice(
        record_id="test",
        access_code_hash="hash",
        document_type="Invoice",
        business_name="Business",
        client_name="Client",
        invoice_number="1",
        issue_date="2026-08-11",
        currency="R",
        vat_percentage="15.00",
        line_items=[{"description": "Work", "rate": "10.10", "quantity": "3.00"}],
        materials=[{"description": "Part", "cost": "5.25"}],
    )
    totals = calculate_invoice(invoice)
    assert totals["subtotal"] == Decimal("30.30")
    assert totals["materials_total"] == Decimal("5.25")
    assert totals["vat_on_line_items"] == Decimal("4.55")
    assert totals["vat_on_materials"] == Decimal("0.79")
    assert totals["vat"] == Decimal("5.33")
    assert totals["total"] == Decimal("40.88")
