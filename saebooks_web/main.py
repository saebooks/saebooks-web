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
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from saebooks_web.auth import router as auth_router
from saebooks_web.config import settings
from saebooks_web.routes.accounts import router as accounts_router
from saebooks_web.routes.bills import router as bills_router
from saebooks_web.routes.contacts import router as contacts_router
from saebooks_web.routes.credit_notes import router as credit_notes_router
from saebooks_web.routes.invoices import router as invoices_router
from saebooks_web.routes.items import router as items_router
from saebooks_web.routes.journal_entries import router as journal_entries_router
from saebooks_web.routes.payments import router as payments_router
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
app.include_router(contacts_router)
app.include_router(invoices_router)
app.include_router(bills_router)
app.include_router(payments_router)
app.include_router(credit_notes_router)
app.include_router(journal_entries_router)
app.include_router(accounts_router)
app.include_router(items_router)
app.include_router(tax_codes_router)


# ---------------------------------------------------------------------------
# Health + root
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is up."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to contacts list (or login if unauthenticated)."""
    return RedirectResponse(url="/contacts", status_code=302)
