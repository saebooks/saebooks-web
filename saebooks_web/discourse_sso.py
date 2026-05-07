"""DiscourseConnect SSO consumer — saebooks-web side.

Mirrors the saebooks-portal flow exactly:
  1. /auth/discourse/login
       - Generate signed nonce, stash in HMAC-signed cookie
       - 303 to discourse.../session/sso_provider?sso=...&sig=...
  2. discourse → /auth/discourse/callback?sso=...&sig=...
  3. Callback:
       - Verify HMAC-SHA256(sso) with DISCOURSE_SSO_SECRET
       - Decode payload, check nonce against stash cookie
       - POST to saebooks-api /api/v1/auth/oauth-handoff with
         provider="discourse", external_user_id=<discourse_id>, email,
         display_name. The handoff secret gates the call.
       - Set request.session["api_token"] like the password flow does
       - Redirect to /
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.config import settings

log = logging.getLogger(__name__)

_NONCE_COOKIE = "sae_discourse_nonce"
_NONCE_TTL_SECONDS = 600  # 10 minutes

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _login_error(request: Request, msg: str, code: int = 400) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/login.html",
        {"error": msg, "discourse_enabled": discourse_enabled()},
        status_code=code,
    )


def discourse_enabled() -> bool:
    return bool(_sso_secret())


def _sso_secret() -> str:
    return os.environ.get("DISCOURSE_SSO_SECRET", "")


def _discourse_base_url() -> str:
    return os.environ.get(
        "DISCOURSE_BASE_URL", "https://discourse.saebooks.com.au"
    ).rstrip("/")


def _public_base_url() -> str:
    return os.environ.get(
        "SAEBOOKS_WEB_PUBLIC_BASE_URL", "https://app.saebooks.com.au"
    ).rstrip("/")


def _hmac(payload: bytes) -> str:
    return hmac.new(_sso_secret().encode(), payload, hashlib.sha256).hexdigest()


def _sign_nonce_cookie(nonce: str, expires_at: int) -> str:
    secret = settings.secret_key.encode()
    payload = f"{nonce}|{expires_at}".encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{nonce}|{expires_at}|{sig_b64}"


def _verify_nonce_cookie(cookie_val: str | None, expected_nonce: str) -> bool:
    if not cookie_val or not expected_nonce:
        return False
    parts = cookie_val.split("|")
    if len(parts) != 3:
        return False
    nonce, exp_str, sig_b64 = parts
    if not hmac.compare_digest(nonce, expected_nonce):
        return False
    try:
        expires_at = int(exp_str)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    secret = settings.secret_key.encode()
    payload = f"{nonce}|{expires_at}".encode()
    expected_sig = hmac.new(secret, payload, hashlib.sha256).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()
    return hmac.compare_digest(sig_b64, expected_sig_b64)


@router.get("/auth/discourse/login")
async def login(request: Request):
    if not discourse_enabled():
        return _login_error(request, "Discourse login is not configured", 503)
    nonce = secrets.token_urlsafe(24)
    return_url = f"{_public_base_url()}/auth/discourse/callback"
    payload = urllib.parse.urlencode({"nonce": nonce, "return_sso_url": return_url}).encode()
    payload_b64 = base64.b64encode(payload).decode()
    sig = _hmac(payload_b64.encode())
    handshake_qs = urllib.parse.urlencode({"sso": payload_b64, "sig": sig})
    target = f"{_discourse_base_url()}/session/sso_provider?{handshake_qs}"

    expires_at = int(time.time()) + _NONCE_TTL_SECONDS
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        _NONCE_COOKIE,
        _sign_nonce_cookie(nonce, expires_at),
        max_age=_NONCE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # TLS terminated upstream; cookie travels first-party
        path="/",
    )
    return response


@router.get("/auth/discourse/callback")
async def callback(
    request: Request,
    sso: str | None = None,
    sig: str | None = None,
):
    if not discourse_enabled():
        return _login_error(request, "Discourse login is not configured", 503)
    if not sso or not sig:
        return _login_error(request, "Missing SSO payload", 400)

    expected_sig = _hmac(sso.encode())
    if not hmac.compare_digest(sig, expected_sig):
        return _login_error(request, "SSO signature mismatch", 400)

    try:
        decoded = base64.b64decode(sso).decode("utf-8")
        params = dict(urllib.parse.parse_qsl(decoded, keep_blank_values=True))
    except Exception:  # noqa: BLE001
        log.exception("failed to decode SSO payload")
        return _login_error(request, "Malformed SSO payload", 400)

    nonce = params.get("nonce")
    if not nonce or not _verify_nonce_cookie(
        request.cookies.get(_NONCE_COOKIE), nonce
    ):
        return _login_error(
            request, "Login expired — please retry from the login page", 400
        )

    external_id = params.get("external_id") or ""
    email = (params.get("email") or "").strip().lower()
    display_name = (
        params.get("name")
        or params.get("username")
        or params.get("email")
        or None
    )

    if not external_id or "@" not in email:
        return _login_error(
            request, "Discourse did not return a usable identity", 400
        )

    handoff_secret = os.environ.get("SAEBOOKS_OAUTH_HANDOFF_SECRET", "")
    if not handoff_secret:
        return _login_error(
            request, "Server not configured (handoff secret missing)", 500
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            handoff = await client.post(
                f"{settings.api_url}/api/v1/auth/oauth-handoff",
                json={
                    "provider": "discourse",
                    "provider_user_id": str(external_id),
                    "email": email,
                    "display_name": display_name,
                },
                headers={"X-OAuth-Handoff-Secret": handoff_secret},
                timeout=15.0,
            )
        except httpx.RequestError:
            return _login_error(request, "Login failed — please try again", 502)
        if not handoff.is_success:
            return _login_error(
                request,
                f"Login failed (handoff status {handoff.status_code})",
                502,
            )
        token = handoff.json().get("access_token")
        if not token:
            return _login_error(request, "Login failed — no token returned", 502)

        request.session.pop("csrf_token", None)
        request.session["api_token"] = token

        try:
            me_resp = await client.get(
                f"{settings.api_url}/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        except httpx.RequestError:
            me_resp = None  # type: ignore[assignment]

    if me_resp is not None and me_resp.is_success:
        profile = me_resp.json()
        request.session["username"] = (
            profile.get("name")
            or profile.get("username")
            or profile.get("email")
            or ""
        )
        request.session["user_role"] = profile.get("role", "")
        allow = frozenset(
            s.strip().lower()
            for s in os.environ.get("SAE_STAFF_USERNAMES", "").split(",")
            if s.strip()
        )
        uname = (profile.get("username") or "").lower()
        uemail = (profile.get("email") or "").lower()
        request.session["is_sae_staff"] = bool(
            allow and (uname in allow or uemail in allow)
        )
    else:
        request.session["is_sae_staff"] = False
        request.session["user_role"] = ""

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(_NONCE_COOKIE, path="/")
    return response
