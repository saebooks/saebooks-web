"""Login / logout routes for the web frontend.

Auth model
----------
POST /api/v1/auth/login accepts ``{"email": str, "password": str}`` and returns
``{"access_token": str, "token_type": "bearer", "expires_in": int}``.  The
returned JWT is stored in the signed session cookie under ``api_token`` — the
same key used everywhere else in the app, so nothing else needs to change.

Error handling:
- 401 from API → re-render login form with "Invalid email or password"
- Any other error / network failure → re-render with generic "Login failed" message
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.config import settings


def _staff_allowlist() -> frozenset[str]:
    raw = os.environ.get("SAE_STAFF_USERNAMES", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())

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
    email: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse | HTMLResponse:
    """Exchange email + password for a JWT; store it in the session."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=5.0,
        ) as client:
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": email.strip(), "password": password},
            )
    except httpx.RequestError:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Login failed — please try again"},
            status_code=502,
        )

    if resp.status_code == 401:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Invalid email or password"},
            status_code=401,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Login failed — please try again"},
            status_code=502,
        )

    token = resp.json()["access_token"]
    request.session["api_token"] = token
    # Fetch user profile to store in session
    try:
        me_resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        if me_resp.is_success:
            profile = me_resp.json()
            request.session["username"] = profile.get("name") or profile.get("email") or ""
            request.session["user_role"] = profile.get("role", "")
            allow = _staff_allowlist()
            uname = (profile.get("username") or "").lower()
            uemail = (profile.get("email") or "").lower()
            request.session["is_sae_staff"] = bool(
                allow and (uname in allow or uemail in allow)
            )
    except Exception:
        pass
    return RedirectResponse(url="/", status_code=303)


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
