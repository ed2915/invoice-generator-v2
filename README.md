# Invoice Generator V2

A persistent Flask invoice and quote generator. Users choose a memorable record ID and can return later with that ID to edit and regenerate the PDF.

## Access model

- Each record ID is unique and cannot be reused for another invoice or quote.
- The record ID is the only credential needed to reopen a saved record. Anyone who knows or guesses it can access the invoice and any details it contains.
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

`render.yaml` defines a separate free web service in Frankfurt. Persistent data is stored in the external Neon Postgres project `invoice-generator-v2-eu`, also in Frankfurt (`aws-eu-central-1`).

Set these private environment variables in Render before deploying:

- `DATABASE_URL`: the pooled Neon connection string. Never commit it to Git.
- `SECRET_KEY`: a long random value used to protect Flask sessions.

The application creates its initial `invoices` table on startup. Future schema changes should use migrations rather than relying on `create_all`.
