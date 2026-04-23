"""Login / logout routes for the web frontend.

Auth model
----------
The saebooks-api (as of Phase 0 / B-cycle 10) uses a static bearer token
loaded from ``SAEBOOKS_DEV_API_TOKEN``.  There is no ``POST /api/v1/auth/login``
endpoint — the token is issued out-of-band and configured by the server operator.

TODO(phase-1): when the portal JWT flow lands (Lane A), replace the token-paste
login below with a proper OIDC / portal credential exchange:
1. POST /api/v1/auth/login with email+password → API returns a short-lived JWT
2. Store the JWT in the session under ``api_token`` (same key, nothing else changes)

For now, the login form accepts the raw API token and verifies it by making a
test call to ``GET /api/v1/contacts?limit=1``.  If the API accepts it, the token
is stored in the signed session cookie.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.config import settings

router = APIRouter()

# Resolve templates relative to the repo root (parent of this package dir).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login form."""
    return _TEMPLATES.TemplateResponse(request, "auth/login.html", {"error": None})


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    api_token: str = Form(...),
) -> RedirectResponse | HTMLResponse:
    """Accept an API token, verify it against the API, store in session.

    Verification is a lightweight ``GET /api/v1/contacts?limit=1`` — if the
    API returns 200 the token is valid; 401 means wrong token.
    """
    # Temporarily inject the token to probe the API.
    test_headers = {"Authorization": f"Bearer {api_token.strip()}"}
    try:
        async with httpx.AsyncClient(
            base_url=settings.api_url,
            headers=test_headers,
            timeout=5.0,
        ) as client:
            resp = await client.get("/api/v1/contacts", params={"limit": 1})
    except httpx.RequestError as exc:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/login.html",
            {"error": f"Cannot reach API server: {exc}"},
            status_code=502,
        )

    if resp.status_code == 401:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Invalid API token."},
            status_code=401,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/login.html",
            {"error": f"Unexpected API response: HTTP {resp.status_code}"},
            status_code=502,
        )

    # Token is valid — store it in the session.
    request.session["api_token"] = api_token.strip()
    return RedirectResponse(url="/contacts", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/logout")
async def logout_get(request: Request) -> RedirectResponse:
    """GET-friendly logout for nav links."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
