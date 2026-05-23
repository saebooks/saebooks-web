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

import base64
import hmac
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pathlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks_web.config import settings

# IMPORTANT: ``saebooks_web.security`` must be imported BEFORE any module
# that constructs a ``fastapi.templating.Jinja2Templates`` instance.  The
# security package patches ``Jinja2Templates.__init__`` on import to
# register ``csrf_input`` as a Jinja global on every new templates env;
# routers imported afterwards therefore get the global automatically.
# The ``ensure_csrf_global`` helper is also exported for the few cases
# where an env was created earlier and needs retrofitting.
from saebooks_web.security import (  # noqa: E402,I001 — placement is load-bearing
    OriginRefererMiddleware,
    CSRFMiddleware,
    ensure_csrf_global,
)
from saebooks_web.security.trusted_header import TrustedHeaderAuthMiddleware  # noqa: E402
from saebooks_web.security.demo_autologin import DemoAutoLoginMiddleware  # noqa: E402

from saebooks_web.auth import router as auth_router
from saebooks_web.discourse_sso import router as discourse_sso_router
from saebooks_web.authentik_sso import router as authentik_sso_router
from saebooks_web.routes.preview import router as preview_router
from saebooks_web.routes.public_auth import router as public_auth_router
from saebooks_web.routes.billing import router as billing_router
from saebooks_web.routes.account_ranges import router as account_ranges_router
from saebooks_web.routes.allocations import router as allocations_router
from saebooks_web.routes.accounts import router as accounts_router
from saebooks_web.routes.admin import router as admin_router
from saebooks_web.routes.ai_extraction import router as ai_extraction_router
from saebooks_web.routes.ato_sbr import router as ato_sbr_router
from saebooks_web.routes.bank_accounts import router as bank_accounts_router
from saebooks_web.routes.bank_rules import router as bank_rules_router
from saebooks_web.routes.bank_statement_lines import router as bank_statement_lines_router
from saebooks_web.routes.bills import router as bills_router
from saebooks_web.routes.expenses import router as expenses_router
from saebooks_web.routes.time_entries import router as time_entries_router
from saebooks_web.routes.budgets import router as budgets_router
from saebooks_web.routes.contacts import router as contacts_router
from saebooks_web.routes.credit_notes import router as credit_notes_router
from saebooks_web.routes.dashboard import router as dashboard_router
from saebooks_web.routes.fixed_assets import router as fixed_assets_router
from saebooks_web.routes.imports import router as imports_router
from saebooks_web.routes.invoices import router as invoices_router
from saebooks_web.routes.email_log import router as email_log_router
from saebooks_web.routes.quotes import router as quotes_router
from saebooks_web.routes.items import router as items_router
from saebooks_web.routes.journal_entries import router as journal_entries_router
from saebooks_web.routes.journal_templates import router as journal_templates_router
from saebooks_web.routes.employees import router as employees_router
from saebooks_web.routes.super_funds import router as super_funds_router
from saebooks_web.routes.pay_run import router as pay_run_router
from saebooks_web.routes.payments import router as payments_router
from saebooks_web.routes.profile import router as profile_router
from saebooks_web.routes.projects import router as projects_router
from saebooks_web.routes.proration import router as proration_router
from saebooks_web.routes.purchase_orders import router as purchase_orders_router
from saebooks_web.routes.reconciliation import router as reconciliation_router
from saebooks_web.routes.recurring_invoices import router as recurring_invoices_router
from saebooks_web.routes.reports import router as reports_router
from saebooks_web.routes.search import router as search_router
from saebooks_web.routes.companies import router as companies_router
from saebooks_web.routes.settings import router as settings_router
from saebooks_web.routes.tax_codes import router as tax_codes_router
from saebooks_web.routes.contact import router as contact_router
from saebooks_web.routes.integrations import router as integrations_router  # Cat-C W6
from saebooks_web.routes.attachments import router as attachments_router  # Phase 1.5
from saebooks_web.routes.pwa import router as pwa_router  # PWA: /sw.js + /manifest.webmanifest
from saebooks_web.routes.cashbook import router as cashbook_router
from saebooks_web.routes.overviews import router as overviews_router  # /sales /expenses /inventory /gst overview dashboards
from saebooks_web.routes.recurring import router as recurring_router  # /recurring aggregator hub
from saebooks_web.routes.cashbook_invoices import router as cashbook_invoices_router
from saebooks_web.routes.cashbook_quotes import router as cashbook_quotes_router

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("saebooks_web")

app = FastAPI(
    title="SAE Books Web",
    description="Thin Jinja2 + HTMX frontend for saebooks-api",
    version="0.1.3",
    docs_url="/api/docs",  # keep /docs free from accidental exposure
    redoc_url=None,
)

# ---------------------------------------------------------------------------
# Static files — built Tailwind CSS (and any future static assets).
# In Docker the file is baked in at /app/static/tailwind.css by the tailwind
# build stage.  For local dev, run `./scripts/build_css.sh --watch` in a
# separate terminal to keep static/tailwind.css up to date.
# ---------------------------------------------------------------------------
_STATIC_DIR = pathlib.Path("/app/static")
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    # Dev fallback — resolve relative to this file's repo root.
    _DEV_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
    if _DEV_STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_DEV_STATIC)), name="static")

# Signed session cookies — secret_key must be set to a strong value in prod.
#
# CSRF defence Layer 1 (P0-3): same_site="strict" prevents the cookie from
# being attached to ANY cross-site request — including embedded form POSTs
# from attacker-controlled pages. With "lax" the cookie is sent on top-level
# cross-site GETs (which is fine) but ALSO on cross-site form POSTs (which
# is the CSRF gap Morgan Chen reproduced).  "strict" closes that hole at the
# browser layer for every modern browser; it is the cheapest and most
# foundational defence.
#
# Tradeoff: a link from email/Slack/external chat to https://books-dev.sauer
# arrives as a cross-site navigation and will NOT carry the cookie, so the
# user is shown the /login page and has to authenticate again.  That is
# acceptable for an admin/accounting tool where every session boundary is
# intentional, and is preferable to leaving CSRF unmitigated.
# CSRF defence Layer 3 (P0-3): per-form CSRF token enforcement.
#
# Middleware-stack ordering note: Starlette's add_middleware does
# ``insert(0, ...)``, so the LAST add_middleware call ends up OUTERMOST
# at request time (executed first per-request, from outside-in).  The
# CSRFMiddleware needs ``scope["session"]`` populated by the
# SessionMiddleware, so it must run INSIDE SessionMiddleware — which
# means it must be added BEFORE SessionMiddleware in code.  Sequence:
#
#   add_middleware(CSRFMiddleware)        — innermost (added first)
#   add_middleware(SessionMiddleware)     — wraps CSRF
#   add_middleware(OriginRefererMiddleware) — wraps Session
#   add_middleware(_RequestIdMiddleware)  — outermost (added last)
#
# Final request flow (outside in): RequestId -> OriginReferer ->
# Session -> CSRF -> route.
app.add_middleware(CSRFMiddleware)

# Authentik forward-auth: mint a session from x-authentik-* headers when
# SAEBOOKS_WEB_TRUSTED_HEADERS=1. Added after CSRF (so it wraps CSRF) and
# before SessionMiddleware (so it runs INSIDE Session and can write to it).
app.add_middleware(TrustedHeaderAuthMiddleware)

# Demo auto-login: cashbook-demo public-demo only — env-gated. Sits
# OUTSIDE TrustedHeaderAuth (added later) so it runs first per
# request, mints a session if creds env vars are set, and lets the
# rest of the stack proceed as if the user manually logged in.
app.add_middleware(DemoAutoLoginMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie_name,
    max_age=settings.session_max_age,
    https_only=False,  # TODO: set True behind TLS reverse proxy in prod
    same_site="strict",
)

# CSRF defence Layer 2 (P0-3): Origin / Referer enforcement on every
# state-changing request.  Mounted AFTER SessionMiddleware so it runs
# OUTSIDE Session (and thus before any route handler).  Rejects with
# 403 + ``code: cross_origin_forbidden`` on any POST/PUT/PATCH/DELETE
# whose Origin or Referer doesn't match SAEBOOKS_WEB_SITE_ORIGIN
# (default https://books-dev.sauer.com.au).  See
# saebooks_web/security/csrf.py for full rules.
app.add_middleware(OriginRefererMiddleware)

# X-Request-Id correlation — generate / propagate a UUID for every
# request. Registered last so it wraps all other middleware and is
# therefore the outermost layer: it sees the final status code from
# inside-out and stamps the header on the response seen by the browser.
# Must be added after OriginRefererMiddleware (which add_middleware
# inserts at index 0, moving this one back to outermost on the way in).
_LOG_ACCESS = logging.getLogger("saebooks_web.access")


class _RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        _LOG_ACCESS.debug(
            "%s %s %s req=%s",
            request.method,
            request.url.path,
            response.status_code,
            request_id,
        )
        return response


app.add_middleware(_RequestIdMiddleware)


# ---------------------------------------------------------------------------
# Preview-build basic auth gate.
#
# Set via env: SAEBOOKS_PREVIEW_BASIC_AUTH="user:password"
# When set, requires HTTP Basic auth on every request except /healthz.
# Used on the unpublished UX-rework preview build so the URL isn't browsable
# by the public while we iterate. Cloudflared tunnel routes around the Caddy
# layer for this hostname, so the gate has to live at the origin.
# ---------------------------------------------------------------------------


class _PreviewBasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, credential: str) -> None:  # noqa: ANN401
        super().__init__(app)
        self._user, _, self._password = credential.partition(":")

    # PWA assets must be accessible without auth so the service worker
    # can fetch its own manifest/icons without triggering an offline-page hijack.
    _BYPASS_PREFIXES = (
        "/healthz",
        "/manifest.webmanifest",
        "/manifest.json",
        "/sw.js",
        "/offline.html",
        "/static/pwa/",
    )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if any(path == p or path.startswith(p) for p in self._BYPASS_PREFIXES):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
                user, _, password = decoded.partition(":")
                if hmac.compare_digest(user, self._user) and hmac.compare_digest(
                    password, self._password
                ):
                    return await call_next(request)
            except Exception:  # noqa: BLE001
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="preview"'},
            content="Authentication required",
        )


_PREVIEW_AUTH = os.getenv("SAEBOOKS_PREVIEW_BASIC_AUTH", "").strip()
if _PREVIEW_AUTH and ":" in _PREVIEW_AUTH:
    app.add_middleware(_PreviewBasicAuthMiddleware, credential=_PREVIEW_AUTH)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(discourse_sso_router)
app.include_router(authentik_sso_router)
app.include_router(public_auth_router)
app.include_router(contact_router)
app.include_router(billing_router)
app.include_router(dashboard_router)
app.include_router(contacts_router)
app.include_router(ai_extraction_router)
app.include_router(invoices_router)
app.include_router(quotes_router)
app.include_router(email_log_router)
app.include_router(bills_router)
app.include_router(expenses_router)
app.include_router(time_entries_router)
app.include_router(purchase_orders_router)
app.include_router(proration_router)
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
app.include_router(allocations_router)
app.include_router(reports_router)
app.include_router(search_router)
app.include_router(profile_router)
app.include_router(companies_router)
app.include_router(settings_router)
app.include_router(employees_router)
app.include_router(super_funds_router)
app.include_router(pay_run_router)
app.include_router(admin_router)
app.include_router(imports_router)
app.include_router(ato_sbr_router)
# Cat-C W6: integrations dashboard + Stripe Connect + LEI/CH HTMX fragments.
app.include_router(integrations_router)
# Phase 1.5: attachment panel (upload / delete / download relay).
app.include_router(attachments_router)
# PWA endpoints (manifest + service worker at origin root).
app.include_router(pwa_router)
# Cashbook UI — single-entry bookkeeping surfaces.
app.include_router(cashbook_router)
app.include_router(cashbook_invoices_router)
app.include_router(cashbook_quotes_router)
# Section overview dashboards — /sales/overview /expenses-overview /inventory/overview /gst/overview
app.include_router(overviews_router)
# Recurring transactions hub — /recurring aggregator over invoices + templates
app.include_router(recurring_router)

# Pass B preview — static design mocks (no data wiring, no auth).
app.include_router(preview_router)


# ---------------------------------------------------------------------------
# OpenAPI filter — strip /admin/* from the published spec.
# The routes still exist and are enforced by session auth checks — they
# just don't advertise themselves as attack targets in the public schema.
# ---------------------------------------------------------------------------

_original_openapi = app.openapi


def _filtered_openapi() -> dict[str, Any]:
    schema = _original_openapi()
    schema["paths"] = {
        path: item
        for path, item in schema.get("paths", {}).items()
        if not path.startswith("/admin/")
    }
    return schema


app.openapi = _filtered_openapi  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is up."""
    return {"status": "ok"}
