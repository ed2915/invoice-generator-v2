# Invoice Generator V2

A persistent Flask invoice and quote generator. Users choose a memorable record ID, receive a private access code, and can return later to edit and regenerate the PDF.

## Security model

- Record IDs are identifiers, not secrets.
- A random 20-character access code protects editing and PDF access.
- Only a password hash is stored in the database.
- CSRF protection, secure response headers, request-size limits, non-negative numeric validation, and item-count limits are built in.
- Invoice contents (including any banking details entered by a user) are stored in the configured PostgreSQL database. Treat database access and backups as sensitive.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
FLASK_ENV=development .venv/bin/flask --app app run --debug
```

Without `DATABASE_URL`, the app uses `invoice_generator.sqlite3`. Production should use PostgreSQL.

## Tests

```bash
.venv/bin/pytest
```

On an Apple Silicon Mac with Pango installed through Homebrew, run the PDF test suite with:

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m pytest
```

## Render deployment

`render.yaml` defines a separate free web service and the smallest persistent paid PostgreSQL configuration in Frankfurt (Basic-256mb compute and 1 GB storage). The free PostgreSQL plan is intentionally not used because Render deletes free databases after 30 days.

Do not apply the Blueprint until the database price has been reviewed and approved.
