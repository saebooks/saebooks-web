"""Browser-side WebAuthn flow handlers for saebooks-web.

The browser POSTs JSON to these routes; we forward to the API
(``/api/v1/auth/webauthn/...``) server-side, attaching the user's
session JWT for authenticated endpoints. The API does verification and
storage; saebooks-web is the user-facing surface and the session-cookie
issuer.

Routes
------

* ``GET  /auth/webauthn/login``                     — passkey landing page
* ``GET  /settings/security``                       — enrollment + mgmt UI (auth required)
* ``POST /auth/webauthn/register/begin``            — proxy → API (auth required)
* ``POST /auth/webauthn/register/finish``           — proxy → API (auth required)
* ``POST /auth/webauthn/authenticate/begin``        — proxy → API (no auth)
* ``POST /auth/webauthn/authenticate/finish``       — proxy → API (no auth), mints session cookie
* ``GET  /auth/webauthn/credentials``               — proxy → API (auth required)
* ``DELETE /auth/webauthn/credentials/{cred_id}``   — proxy → API (auth required)
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.webauthn_sso")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def webauthn_enabled() -> bool:
    """Mirror the API's feature flag — UI hides the button when disabled."""
    return os.environ.get("SAEBOOKS_WEBAUTHN_ENABLED", "1").strip().lower() in (
        "1", "true", "yes",
    )


def _staff_allowlist() -> frozenset[str]:
    raw = os.environ.get("SAE_STAFF_USERNAMES", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _require_session_token(request: Request) -> str:
    token = request.session.get("api_token")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")
    return token


async def _proxy_post(path: str, body: Any, bearer: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(base_url=settings.api_url, timeout=15.0) as client:
        resp = await client.post(path, json=body, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.is_success:
            detail = (data or {}).get("detail", f"HTTP {resp.status_code}")
            raise HTTPException(resp.status_code, detail)
        return data


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@router.get("/auth/webauthn/login", response_class=HTMLResponse, response_model=None)
async def webauthn_login_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not webauthn_enabled():
        return RedirectResponse(url="/login?form=1", status_code=303)
    if request.session.get("api_token"):
        return RedirectResponse(url="/", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request, "auth/webauthn_login.html", {"error": None},
    )


@router.get("/settings/security", response_class=HTMLResponse, response_model=None)
async def security_settings(request: Request) -> HTMLResponse | RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "settings/security.html",
        {
            "webauthn_enabled": webauthn_enabled(),
            "rp_id": os.environ.get("SAEBOOKS_WEBAUTHN_RP_ID", ""),
        },
    )


# --------------------------------------------------------------------------
# JSON proxies — invoked from browser fetch()
# --------------------------------------------------------------------------


class RegBeginRequest(BaseModel):
    pass


class RegFinishRequest(BaseModel):
    credential: dict
    friendly_name: str = "Security key"


class AuthBeginRequest(BaseModel):
    pass


class AuthFinishRequest(BaseModel):
    credential: dict


@router.post("/auth/webauthn/register/begin")
async def webauthn_register_begin(_body: RegBeginRequest, request: Request) -> JSONResponse:
    if not webauthn_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webauthn_disabled")
    token = _require_session_token(request)
    data = await _proxy_post("/api/v1/auth/webauthn/register/begin", {}, token)
    return JSONResponse(data)


@router.post("/auth/webauthn/register/finish")
async def webauthn_register_finish(body: RegFinishRequest, request: Request) -> JSONResponse:
    if not webauthn_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webauthn_disabled")
    token = _require_session_token(request)
    data = await _proxy_post(
        "/api/v1/auth/webauthn/register/finish",
        {"credential": body.credential, "friendly_name": body.friendly_name},
        token,
    )
    return JSONResponse(data)


@router.post("/auth/webauthn/authenticate/begin")
async def webauthn_authenticate_begin(_body: AuthBeginRequest) -> JSONResponse:
    if not webauthn_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webauthn_disabled")
    data = await _proxy_post("/api/v1/auth/webauthn/authenticate/begin", {}, None)
    return JSONResponse(data)


@router.post("/auth/webauthn/authenticate/finish")
async def webauthn_authenticate_finish(body: AuthFinishRequest, request: Request) -> JSONResponse:
    """Verify the assertion via the API, then turn the returned JWT into a
    saebooks-web session cookie."""
    if not webauthn_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webauthn_disabled")
    data = await _proxy_post(
        "/api/v1/auth/webauthn/authenticate/finish",
        {"credential": body.credential},
        None,
    )
    token = data.get("access_token", "")
    if not token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "no_token_returned")

    # Mint a saebooks-web session cookie from the JWT
    request.session.pop("csrf_token", None)
    request.session["api_token"] = token

    # Hydrate user profile so the dashboard renders nicely on first hit
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=5.0) as client:
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
                allow = _staff_allowlist()
                uname = (profile.get("username") or "").lower()
                uemail = (profile.get("email") or "").lower()
                request.session["is_sae_staff"] = bool(
                    allow and (uname in allow or uemail in allow)
                )
            else:
                request.session["is_sae_staff"] = False
                request.session["user_role"] = ""
    except httpx.RequestError as exc:
        logger.warning("webauthn /auth/me fetch failed: %s", exc)
        request.session["is_sae_staff"] = False
        request.session["user_role"] = ""

    return JSONResponse({"ok": True, "redirect": "/"})


@router.get("/auth/webauthn/credentials")
async def webauthn_list_credentials(request: Request) -> JSONResponse:
    if not webauthn_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webauthn_disabled")
    token = _require_session_token(request)
    async with httpx.AsyncClient(base_url=settings.api_url, timeout=10.0) as client:
        resp = await client.get(
            "/api/v1/auth/webauthn/credentials",
            headers={"Authorization": f"Bearer {token}"},
        )
        if not resp.is_success:
            try:
                detail = resp.json().get("detail", f"HTTP {resp.status_code}")
            except Exception:
                detail = f"HTTP {resp.status_code}"
            raise HTTPException(resp.status_code, detail)
        return JSONResponse(resp.json())


@router.delete("/auth/webauthn/credentials/{cred_id}")
async def webauthn_delete_credential(cred_id: uuid.UUID, request: Request) -> JSONResponse:
    if not webauthn_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webauthn_disabled")
    token = _require_session_token(request)
    async with httpx.AsyncClient(base_url=settings.api_url, timeout=10.0) as client:
        resp = await client.delete(
            f"/api/v1/auth/webauthn/credentials/{cred_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code not in (200, 204):
            try:
                detail = resp.json().get("detail", f"HTTP {resp.status_code}")
            except Exception:
                detail = f"HTTP {resp.status_code}"
            raise HTTPException(resp.status_code, detail)
        return JSONResponse({"ok": True})
