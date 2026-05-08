"""Web routes for the integrations settings page.

Provides a thin SSR wrapper around the ``/api/v1/integrations/*``
API endpoints.  All data access goes through the saebooks-api REST
API via ``api_client`` — this module does not touch the database
directly.

Routes
------
GET  /settings/integrations
    Renders the integrations dashboard — status badges for each
    configured integration plus links to sub-configuration pages.
    Feature-gated sections are rendered conditionally based on the
    tenant's edition (read from the session JWT claims).

POST /settings/integrations/stripe/connect
    Proxy the customer Stripe Connect initiation — calls
    ``POST /api/v1/integrations/stripe/customer/connect`` and
    redirects the browser to the returned ``authorize_url``.

GET  /settings/integrations/stripe/status
    HTMX fragment endpoint — returns a partial with the current
    Stripe Connect status badge.

POST /settings/integrations/lei/lookup
    HTMX fragment — calls ``POST /api/v1/integrations/lei/lookup``
    and returns a rendered result or error partial.

POST /settings/integrations/companies-house/search
    HTMX fragment — calls ``POST /api/v1/integrations/companies-house/search``
    and returns a result partial.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

logger = logging.getLogger("saebooks_web.integrations")

router = APIRouter(prefix="/settings/integrations")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _render(request: Request, template: str, ctx: dict[str, Any] | None = None) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, template, ctx or {})


# ---------------------------------------------------------------------------
# Integration dashboard
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def integrations_index(request: Request) -> HTMLResponse:
    """Render the integrations landing page with status badges."""
    stripe_status: dict[str, Any] = {"connected": False}
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/integrations/stripe/customer")
            if resp.status_code == 200:
                stripe_status = resp.json()
            elif resp.status_code == 404:
                # Feature flag not enabled — stripe integration not available
                stripe_status = {"connected": False, "_unavailable": True}
    except httpx.HTTPError as exc:
        logger.warning("integrations: stripe status fetch error: %s", exc)

    xero_status = await _fetch_xero_status(request)

    return _render(
        request,
        "integrations/index.html",
        {
            "stripe_status": stripe_status,
            "xero_status": xero_status,
            "page_title": "Integrations",
        },
    )


# ---------------------------------------------------------------------------
# Xero accounting-package sync (Enterprise)
# ---------------------------------------------------------------------------


async def _fetch_xero_status(request: Request) -> dict[str, Any]:
    """Resolve Xero connection state for the current tenant.

    Returns one of:

    * ``{"connected": False, "_unavailable": True}`` — feature flag off
    * ``{"connected": False}`` — flag on, no connection rows
    * ``{"connected": True, "connection": <row>}`` — at least one row;
      the most recent connection (active/error/revoked) is exposed to
      the template.
    """
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/sync/xero/status")
            if resp.status_code == 404:
                # Feature flag not enabled
                return {"connected": False, "_unavailable": True}
            if resp.status_code != 200:
                logger.warning(
                    "integrations: xero status non-200: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return {"connected": False}
            rows = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("integrations: xero status fetch error: %s", exc)
        return {"connected": False}

    if not rows:
        return {"connected": False}

    # Pick the most recent — by created_at desc if present, else first.
    rows_sorted = sorted(
        rows,
        key=lambda r: r.get("created_at") or "",
        reverse=True,
    )
    return {"connected": True, "connection": rows_sorted[0]}


@router.get("/xero/status", response_class=HTMLResponse)
async def xero_status_fragment(request: Request) -> HTMLResponse:
    """HTMX fragment — current Xero connection badge + actions."""
    xero_status = await _fetch_xero_status(request)
    return _render(
        request,
        "integrations/_xero_status.html",
        {"xero_status": xero_status},
    )


@router.post("/xero/connect")
async def xero_connect_initiate(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(...),
    redirect_uri: str = Form(...),
) -> RedirectResponse:
    """Start the Xero OAuth consent flow.

    Calls ``POST /api/v1/sync/xero/connect`` with the operator-supplied
    client credentials, then redirects the browser to Xero's authorise
    URL. On consent, Xero sends the operator back to ``redirect_uri``
    which must be wired to the API's ``/api/v1/sync/xero/callback``.
    """
    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/sync/xero/connect",
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "integrations: xero connect initiation failed: %s %s",
            exc.response.status_code,
            exc.response.text[:200],
        )
        return RedirectResponse(
            url="/settings/integrations?error=xero_connect_failed",
            status_code=303,
        )
    except httpx.HTTPError as exc:
        logger.error("integrations: xero connect transport error: %s", exc)
        return RedirectResponse(
            url="/settings/integrations?error=xero_connect_unavailable",
            status_code=303,
        )

    authorize_url = data.get("authorize_url", "")
    if not authorize_url:
        return RedirectResponse(
            url="/settings/integrations?error=xero_connect_failed",
            status_code=303,
        )

    return RedirectResponse(url=authorize_url, status_code=303)


@router.post("/xero/{connection_id}/trigger", response_class=HTMLResponse)
async def xero_trigger_sync(
    request: Request,
    connection_id: str,
) -> HTMLResponse:
    """HTMX: kick a synchronous sync run, then re-render the status fragment."""
    try:
        async with api_client(request) as client:
            await client.post(
                f"/api/v1/sync/xero/{connection_id}/trigger",
                timeout=120.0,
            )
    except httpx.HTTPError as exc:
        logger.warning("integrations: xero trigger error: %s", exc)

    xero_status = await _fetch_xero_status(request)
    return _render(
        request,
        "integrations/_xero_status.html",
        {"xero_status": xero_status},
    )


@router.delete("/xero/{connection_id}", response_class=HTMLResponse)
async def xero_disconnect(
    request: Request,
    connection_id: str,
) -> HTMLResponse:
    """HTMX: revoke a Xero connection and re-render the status fragment."""
    try:
        async with api_client(request) as client:
            await client.delete(f"/api/v1/sync/xero/{connection_id}")
    except httpx.HTTPError as exc:
        logger.warning("integrations: xero disconnect error: %s", exc)

    xero_status = await _fetch_xero_status(request)
    return _render(
        request,
        "integrations/_xero_status.html",
        {"xero_status": xero_status},
    )


# ---------------------------------------------------------------------------
# Stripe Connect
# ---------------------------------------------------------------------------


@router.post("/stripe/connect")
async def stripe_connect_initiate(request: Request) -> RedirectResponse:
    """Initiate the Stripe Connect OAuth flow.

    Calls the API to get the Stripe authorise URL, then redirects
    the user's browser there. On success, Stripe sends the user back
    to the registered callback URL.
    """
    try:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/integrations/stripe/customer/connect")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("integrations: stripe connect initiation failed: %s", exc)
        return RedirectResponse(
            url="/settings/integrations?error=stripe_connect_failed",
            status_code=303,
        )

    authorize_url = data.get("authorize_url", "")
    if not authorize_url:
        return RedirectResponse(
            url="/settings/integrations?error=stripe_connect_failed",
            status_code=303,
        )

    # Stash state in session for callback validation (future build).
    request.session["stripe_connect_state"] = data.get("state", "")

    return RedirectResponse(url=authorize_url, status_code=303)


@router.get("/stripe/status", response_class=HTMLResponse)
async def stripe_connect_status_fragment(request: Request) -> HTMLResponse:
    """HTMX fragment — current Stripe Connect status badge."""
    stripe_status: dict[str, Any] = {"connected": False}
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/integrations/stripe/customer")
            if resp.status_code == 200:
                stripe_status = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("integrations: stripe status error: %s", exc)

    return _render(
        request,
        "integrations/_stripe_status.html",
        {"stripe_status": stripe_status},
    )


# ---------------------------------------------------------------------------
# LEI lookup (HTMX fragment)
# ---------------------------------------------------------------------------


@router.post("/lei/lookup", response_class=HTMLResponse)
async def lei_lookup_fragment(
    request: Request,
    search: str = Form(...),
) -> HTMLResponse:
    """HTMX: look up an LEI and render a result or error partial."""
    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/integrations/lei/lookup",
                json={"search": search},
            )
            if resp.status_code == 404:
                return _render(
                    request,
                    "integrations/_lei_error.html",
                    {"message": resp.json().get("detail", "No entity found for that LEI")},
                )
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPStatusError as exc:
        return _render(
            request,
            "integrations/_lei_error.html",
            {"message": f"LEI lookup failed: {exc.response.status_code}"},
        )
    except httpx.HTTPError as exc:
        return _render(
            request,
            "integrations/_lei_error.html",
            {"message": f"LEI lookup unavailable: {exc}"},
        )

    return _render(request, "integrations/_lei_result.html", {"result": result})


# ---------------------------------------------------------------------------
# Companies House search (HTMX fragment)
# ---------------------------------------------------------------------------


@router.post("/companies-house/search", response_class=HTMLResponse)
async def companies_house_search_fragment(
    request: Request,
    query: str = Form(...),
) -> HTMLResponse:
    """HTMX: search Companies House and render a result or error partial."""
    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/integrations/companies-house/search",
                json={"query": query},
            )
            if resp.status_code == 404:
                return _render(
                    request,
                    "integrations/_ch_error.html",
                    {"message": "No company found for that query"},
                )
            if resp.status_code == 503:
                return _render(
                    request,
                    "integrations/_ch_error.html",
                    {"message": "Companies House API is not configured"},
                )
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPStatusError as exc:
        return _render(
            request,
            "integrations/_ch_error.html",
            {"message": f"Companies House search failed: {exc.response.status_code}"},
        )
    except httpx.HTTPError as exc:
        return _render(
            request,
            "integrations/_ch_error.html",
            {"message": f"Companies House unavailable: {exc}"},
        )

    return _render(request, "integrations/_ch_result.html", {"result": result})
