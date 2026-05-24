"""Public-tier auth flows — signup, email verification, password reset, magic link.

These endpoints are reachable without an existing session. They proxy
to the saebooks-api ``/auth/*`` endpoints and, on success, drop the
returned JWT into the session cookie under ``api_token`` — exactly
like ``/login``. From that point the user is signed in.

Pages
-----
* ``GET  /signup``                  — render the signup form
* ``POST /signup``                  — submit; on success show check-email
* ``GET  /verify-email?token=…``    — exchange verification token for JWT,
                                      log the user in, redirect to /
* ``GET  /forgot-password``         — render request form
* ``POST /forgot-password``         — submit; show generic confirmation
* ``GET  /reset-password?token=…``  — render new-password form (token in URL)
* ``POST /reset-password``          — submit; on success log in + redirect
* ``GET  /magic-link?token=…``      — exchange magic-link token for JWT,
                                      log in, redirect

CSRF
----
GET pages render forms with ``{{ csrf_input() }}``.  The standard
``CSRFMiddleware`` enforces the token on all of these POSTs except
those listed in ``_TOKEN_SKIP_PATHS`` in ``security/csrf.py``.
``verify-email`` and ``magic-link`` are GET-with-token so are exempt by
HTTP semantics.

Error rendering
---------------
The API surfaces Pydantic-style detail strings on 4xx — we map known
codes ("email_exists", "weak_password", "invalid_or_expired_token") to
human messages and fall back to a generic banner.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.public_auth")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.api_url, timeout=10.0)


def _format_api_error(resp: httpx.Response, fallback: str) -> str:
    """Pull a human-readable error out of the API's problem-details JSON."""
    try:
        data = resp.json()
    except Exception:  # pragma: no cover - defensive
        return fallback
    detail = data.get("detail") or data.get("title") or ""
    detail = str(detail).strip()
    code = (data.get("code") or "").lower()
    mapping = {
        "email_exists": "An account with that email already exists. Try signing in or use the reset link.",
        "weak_password": "Password must be at least 10 characters and contain a letter and a digit.",
        "invalid_email": "That doesn't look like a valid email address.",
        "rate_limited": "Too many attempts — please wait a minute and try again.",
        "invalid_or_expired_token": "That link has expired or has already been used. Request a new one.",
        "email_not_verified": "You need to verify your email first. Check your inbox or request a new link.",
    }
    if code in mapping:
        return mapping[code]
    if detail:
        return detail
    return fallback


async def _login_with_jwt(request: Request, token: str) -> None:
    """Drop the JWT into the session and fetch profile info — mirror of
    the post-login bookkeeping in ``saebooks_web.auth.login_submit``."""
    request.session.pop("csrf_token", None)
    request.session.pop("active_company_id", None)
    request.session["api_token"] = token
    try:
        async with _api_client() as client:
            me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        if me.is_success:
            profile = me.json()
            request.session["username"] = (
                profile.get("name")
                or profile.get("username")
                or profile.get("email")
                or ""
            )
            request.session["user_role"] = profile.get("role", "")
            request.session["is_sae_staff"] = False
    except httpx.RequestError as exc:
        logger.warning("public_auth: /auth/me fetch failed: %s", exc)


# ---------------------------------------------------------------------------
# /signup
# ---------------------------------------------------------------------------


async def _fetch_promo_stats() -> dict:
    """Fetch promo stats from the API (server-side, cached per-render).

    Returns a safe dict even when the API is unreachable. The template
    checks ``promo.enabled`` before rendering any promo UI.
    """
    if not settings.launch_promo_enabled:
        return {"enabled": False, "issued": 0, "limit": 1000, "remaining": 1000}
    try:
        async with _api_client() as client:
            resp = await client.get("/api/v1/license/promo-stats", timeout=3.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {"enabled": settings.launch_promo_enabled, "issued": 0, "limit": 1000, "remaining": 1000}


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, plan: str | None = None) -> HTMLResponse:
    _VALID_PLANS = {"business", "pro", "enterprise"}
    safe_plan = plan if plan in _VALID_PLANS else None
    promo = await _fetch_promo_stats()
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/signup.html",
        {"error": None, "values": {"plan": safe_plan}, "promo": promo},
    )


@router.post("/signup", response_model=None)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    company_name: str = Form(...),
    name: str | None = Form(default=None),
    plan: str | None = Form(default=None),
) -> HTMLResponse:
    _VALID_PLANS = {"business", "pro", "enterprise"}
    safe_plan: str | None = plan if plan in _VALID_PLANS else None
    payload: dict[str, Any] = {
        "email": email.strip(),
        "password": password,
        "company_name": company_name.strip(),
    }
    if name and name.strip():
        payload["name"] = name.strip()
    if safe_plan:
        payload["plan"] = safe_plan
    try:
        async with _api_client() as client:
            resp = await client.post("/api/v1/auth/signup", json=payload)
    except httpx.RequestError:
        promo = await _fetch_promo_stats()
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/signup.html",
            {
                "error": "Couldn't reach the server. Please try again in a moment.",
                "values": {"email": email, "company_name": company_name, "name": name or "", "plan": safe_plan},
                "promo": promo,
            },
            status_code=502,
        )
    if resp.status_code == 201:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/check_email.html",
            {"email": email.strip(), "kind": "verification"},
        )
    promo = await _fetch_promo_stats()
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/signup.html",
        {
            "error": _format_api_error(resp, "Sign-up failed — please try again."),
            "values": {"email": email, "company_name": company_name, "name": name or "", "plan": safe_plan},
            "promo": promo,
        },
        status_code=resp.status_code if resp.status_code < 500 else 502,
    )


# ---------------------------------------------------------------------------
# /verify-email
# ---------------------------------------------------------------------------


@router.get("/verify-email", response_model=None)
async def verify_email(request: Request, token: str | None = None) -> Any:
    if not token:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_email_error.html",
            {"message": "This verification link is malformed."},
            status_code=400,
        )
    try:
        async with _api_client() as client:
            resp = await client.post(
                "/api/v1/auth/verify-email", json={"token": token}
            )
    except httpx.RequestError:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_email_error.html",
            {"message": "Couldn't reach the server. Please try again."},
            status_code=502,
        )
    if resp.is_success:
        body = resp.json()
        access = body.get("access_token")
        if access:
            await _login_with_jwt(request, access)
            pending_plan = body.get("signup_plan")
            if pending_plan:
                return RedirectResponse(
                    url=f"/billing/checkout?plan={pending_plan}", status_code=303
                )
            return RedirectResponse(url="/", status_code=303)
        # Successful verify without a token shouldn't happen, but degrade
        # gracefully — send them to login.
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/verify_email_error.html",
        {
            "message": _format_api_error(
                resp,
                "That verification link has expired. Request a new one below.",
            ),
        },
        status_code=resp.status_code if resp.status_code < 500 else 502,
    )


# ---------------------------------------------------------------------------
# /forgot-password
# ---------------------------------------------------------------------------


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/forgot_password.html",
        {"error": None, "submitted": False, "email": ""},
    )


@router.post("/forgot-password", response_model=None)
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
) -> HTMLResponse:
    try:
        async with _api_client() as client:
            await client.post(
                "/api/v1/auth/password-reset/request",
                json={"email": email.strip()},
            )
    except httpx.RequestError:
        # Fail open — never expose backend availability state to the
        # caller for password-reset.  Always show the same banner.
        logger.warning("public_auth: password-reset/request unreachable")
    # Always show the generic confirmation regardless of whether the
    # email exists — this matches the API's enumeration-safe behaviour.
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/forgot_password.html",
        {"error": None, "submitted": True, "email": email.strip()},
    )


# ---------------------------------------------------------------------------
# /reset-password
# ---------------------------------------------------------------------------


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str | None = None) -> HTMLResponse:
    if not token:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_email_error.html",
            {"message": "This reset link is malformed."},
            status_code=400,
        )
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/reset_password.html",
        {"error": None, "token": token},
    )


@router.post("/reset-password", response_model=None)
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
) -> Any:
    try:
        async with _api_client() as client:
            resp = await client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"token": token, "password": password},
            )
    except httpx.RequestError:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/reset_password.html",
            {
                "error": "Couldn't reach the server. Please try again.",
                "token": token,
            },
            status_code=502,
        )
    if resp.is_success:
        body = resp.json()
        access = body.get("access_token")
        if access:
            await _login_with_jwt(request, access)
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/reset_password.html",
        {
            "error": _format_api_error(
                resp, "That reset link has expired. Request a new one."
            ),
            "token": token,
        },
        status_code=resp.status_code if resp.status_code < 500 else 502,
    )


# ---------------------------------------------------------------------------
# /magic-link
# ---------------------------------------------------------------------------


@router.get("/magic-link", response_model=None)
async def magic_link(request: Request, token: str | None = None) -> Any:
    if not token:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_email_error.html",
            {"message": "This magic link is malformed."},
            status_code=400,
        )
    try:
        async with _api_client() as client:
            resp = await client.post(
                "/api/v1/auth/magic-link/consume",
                json={"token": token},
            )
    except httpx.RequestError:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_email_error.html",
            {"message": "Couldn't reach the server. Please try again."},
            status_code=502,
        )
    if resp.is_success:
        body = resp.json()
        access = body.get("access_token")
        if access:
            await _login_with_jwt(request, access)
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/verify_email_error.html",
        {
            "message": _format_api_error(
                resp, "That magic link has expired. Request a new one."
            ),
        },
        status_code=resp.status_code if resp.status_code < 500 else 502,
    )


# ---------------------------------------------------------------------------
# GET /promo-stats-partial — HTMX fragment for the banner counter
# ---------------------------------------------------------------------------
# The signup banner polls this every 60 s via hx-get to refresh the
# "N spots left" number without reloading the whole page.
# Returns the full banner div (outerHTML swap).


@router.get("/promo-stats-partial", response_class=HTMLResponse)
async def promo_stats_partial(request: Request) -> HTMLResponse:
    promo = await _fetch_promo_stats()
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/_promo_banner.html",
        {"promo": promo},
    )
