# saebooks-web

Server-side-rendered web frontend for [saebooks](../saebooks/) — the SAE Books
ledger UI. Built on FastAPI + Jinja2 + HTMX: no Node.js, no SPA framework,
no build step.

Licensed AGPLv3 + commercial dual licence, same as the engine — the frontend is
open so the community can read, audit, and modify the screens they use every day.

## What it is

`saebooks-web` is a **thin client over the public API**. It:

1. Holds the user's session (signed cookie via `itsdangerous`)
2. Proxies all data requests to `saebooks` via `httpx`, injecting the bearer token
3. Renders Jinja2 templates server-side, with HTMX fragments for interactivity

The web server never talks to the database directly. All business logic lives in
the `saebooks` engine — this is deliberately a presentation layer over the same
OpenAPI endpoints that scripts and integrations call.

## Requirements

- Python 3.12+
- A running `saebooks` API instance (default: `http://localhost:8042`)
- The API token set via `SAEBOOKS_DEV_API_TOKEN` on the API server

## Running in development

```bash
# Install dependencies
uv sync --extra dev

# Run (hot-reload)
uv run uvicorn saebooks_web.main:app --reload --port 8043
```

Then open `http://localhost:8043` and sign in (API token in dev; portal
email+password in production — see `auth.py` / `public_auth.py`).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SAEBOOKS_WEB_API_URL` | `http://localhost:8042` | Base URL of the saebooks API |
| `SAEBOOKS_WEB_SECRET_KEY` | `dev-insecure-change-me-before-prod` | Session signing key — **must** be changed in production |
| `SAEBOOKS_WEB_PORT` | `8043` | Port to bind to |
| `SAEBOOKS_WEB_HOST` | `0.0.0.0` | Bind address |
| `SAEBOOKS_WEB_DEBUG` | `false` | Enable debug mode |
| `SAEBOOKS_WEB_LOG_LEVEL` | `INFO` | Log level |

Generate a production secret key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Running tests

```bash
uv run pytest
```

## Project layout

```
saebooks_web/
  main.py          FastAPI app + middleware wiring
  config.py        Pydantic-settings (env vars)
  auth.py          API-token login/logout
  public_auth.py   Portal email+password session auth
  api_client.py    httpx wrapper (injects bearer token from session)
  routes/          one module per surface — the UI mirrors the API
    sales:      invoices, quotes, recurring_invoices, credit_notes, payments,
                allocations, parties, contacts
    purchases:  bills, expenses, purchase_orders, items
    banking:    bank_accounts, bank_statement_lines, bank_rules,
                reconciliation, imports
    ledger:     accounts, account_ranges, journal_entries, journal_templates,
                tax_codes, budgets, reports, overviews
    payroll:    employees, pay_run, time_entries, super_funds
    assets/tax: fixed_assets, ato_sbr, proration
    cashbook:   cashbook, cashbook_invoices, cashbook_quotes
    platform:   companies, branches, switch_company, settings, profile,
                billing, integrations, attachments, search, dashboard, admin*
templates/         Jinja2 layouts + HTMX fragments per surface
tests/             respx-mocked API, render + smoke tests
```

## Architecture note

The web tier carries no business rules. Posting logic, validation, tenant
isolation, and tax computation all live in the `saebooks` engine; the frontend
only renders what the API returns and posts back what the user submits. This
keeps the public REST API the single source of truth — the browser is just one
client of it.
