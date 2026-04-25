"""SAE Books web frontend — FastAPI application factory.

Architecture
------------
This is a thin SSR client that proxies all data access through the
saebooks-api REST API (port 8042 by default).  It does NOT talk to the
database directly.

Stack:
- FastAPI + Jinja2 for server-side rendering
- HTMX (CDN) for fragment-swap interactivity
- itsdangerous SessionMiddleware for signed session cookies
- httpx (async) for upstream API calls via api_client.py

Running
-------
    uvicorn saebooks_web.main:app --reload --port 8043

Or via the helper in pyproject.toml::

    uv run uvicorn saebooks_web.main:app --reload --port 8043

Environment variables: see config.py / README.md.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from saebooks_web.auth import router as auth_router
from saebooks_web.config import settings
from saebooks_web.routes.account_ranges import router as account_ranges_router
from saebooks_web.routes.accounts import router as accounts_router
from saebooks_web.routes.bank_accounts import router as bank_accounts_router
from saebooks_web.routes.bank_rules import router as bank_rules_router
from saebooks_web.routes.bank_statement_lines import router as bank_statement_lines_router
from saebooks_web.routes.reconciliation import router as reconciliation_router
from saebooks_web.routes.bills import router as bills_router
from saebooks_web.routes.budgets import router as budgets_router
from saebooks_web.routes.contacts import router as contacts_router
from saebooks_web.routes.credit_notes import router as credit_notes_router
from saebooks_web.routes.dashboard import router as dashboard_router
from saebooks_web.routes.fixed_assets import router as fixed_assets_router
from saebooks_web.routes.invoices import router as invoices_router
from saebooks_web.routes.items import router as items_router
from saebooks_web.routes.journal_entries import router as journal_entries_router
from saebooks_web.routes.journal_templates import router as journal_templates_router
from saebooks_web.routes.payments import router as payments_router
from saebooks_web.routes.projects import router as projects_router
from saebooks_web.routes.recurring_invoices import router as recurring_invoices_router
from saebooks_web.routes.reports import router as reports_router
from saebooks_web.routes.search import router as search_router
from saebooks_web.routes.admin import router as admin_router
from saebooks_web.routes.ai_extraction import router as ai_extraction_router
from saebooks_web.routes.ato_sbr import router as ato_sbr_router
from saebooks_web.routes.imports import router as imports_router
from saebooks_web.routes.pay_run import router as pay_run_router
from saebooks_web.routes.settings import router as settings_router
from saebooks_web.routes.tax_codes import router as tax_codes_router

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("saebooks_web")

app = FastAPI(
    title="SAE Books Web",
    description="Thin Jinja2 + HTMX frontend for saebooks-api",
    version="0.1.0",
    docs_url="/api/docs",  # keep /docs free from accidental exposure
    redoc_url=None,
)

# Signed session cookies — secret_key must be set to a strong value in prod.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie_name,
    max_age=settings.session_max_age,
    https_only=False,  # TODO: set True behind TLS reverse proxy in prod
    same_site="lax",
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(contacts_router)
app.include_router(ai_extraction_router)
app.include_router(invoices_router)
app.include_router(bills_router)
app.include_router(payments_router)
app.include_router(credit_notes_router)
app.include_router(journal_entries_router)
app.include_router(journal_templates_router)
app.include_router(accounts_router)
app.include_router(account_ranges_router)
app.include_router(items_router)
app.include_router(tax_codes_router)
app.include_router(projects_router)
app.include_router(fixed_assets_router)
app.include_router(recurring_invoices_router)
app.include_router(bank_accounts_router)
app.include_router(bank_rules_router)
app.include_router(bank_statement_lines_router)
app.include_router(reconciliation_router)
app.include_router(budgets_router)
app.include_router(reports_router)
app.include_router(search_router)
app.include_router(settings_router)
app.include_router(pay_run_router)
app.include_router(admin_router)
app.include_router(imports_router)
app.include_router(ato_sbr_router)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is up."""
    return {"status": "ok"}
