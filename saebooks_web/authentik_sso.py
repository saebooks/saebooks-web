"""Authentik OIDC SSO consumer — saebooks-web side.

Mirrors the discourse_sso.py pattern but speaks OIDC (Authorization Code +
PKCE) against an Authentik provider. Used by the internal
books.sauer.com.au instance to delegate login to the sauer.com.au
Authentik IdP. Public app.saebooks.com.au keeps Discourse SSO and stays
off Authentik per the saebooks-identity boundary.

Flow:
  1. /auth/authentik/login
       - Generate state + PKCE verifier, stash in HMAC-signed cookie
       - 303 to AUTHENTIK_AUTHORIZE_URL with code_challenge + state
  2. authentik → /auth/authentik/callback?code=...&state=...
  3. Callback:
       - Verify state cookie
       - POST code + code_verifier to AUTHENTIK_TOKEN_URL with basic-auth
         (client_id + client_secret)
       - GET AUTHENTIK_USERINFO_URL with the access_token
       - POST to saebooks /api/v1/auth/oauth-handoff with
         provider="authentik", provider_user_id=<sub>, email, display_name
       - Set request.session["api_token"]
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
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.config import settings

log = logging.getLogger(__name__)

_STATE_COOKIE = "sae_authentik_state"
_STATE_TTL_SECONDS = 600  # 10 minutes

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def authentik_enabled() -> bool:
    """True when every required Authentik OIDC env var is set."""
    return bool(
        os.environ.get("AUTHENTIK_CLIENT_ID")
        and os.environ.get("AUTHENTIK_CLIENT_SECRET")
        and os.environ.get("AUTHENTIK_AUTHORIZE_URL")
        and os.environ.get("AUTHENTIK_TOKEN_URL")
        and os.environ.get("AUTHENTIK_USERINFO_URL")
    )


def _client_id() -> str:
    return os.environ.get("AUTHENTIK_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("AUTHENTIK_CLIENT_SECRET", "")


def _authorize_url() -> str:
    return os.environ.get("AUTHENTIK_AUTHORIZE_URL", "").rstrip("?")


def _token_url() -> str:
    return os.environ.get("AUTHENTIK_TOKEN_URL", "")


def _userinfo_url() -> str:
    return os.environ.get("AUTHENTIK_USERINFO_URL", "")


def _button_label() -> str:
    return os.environ.get("AUTHENTIK_BUTTON_LABEL", "Continue with SAE Engineering")


def _public_base_url() -> str:
    return os.environ.get(
        "SAEBOOKS_WEB_PUBLIC_BASE_URL", "https://books.sauer.com.au"
    ).rstrip("/")


def _login_error(request: Request, msg: str, code: int = 400) -> HTMLResponse:
    # Lazy import to avoid a circular ref with discourse_sso/auth.
    from saebooks_web.discourse_sso import discourse_enabled
    from saebooks_web.eid_sso import eid_enabled

    return _TEMPLATES.TemplateResponse(
        request,
        "auth/login.html",
        {
            "error": msg,
            "discourse_enabled": discourse_enabled(),
            "authentik_enabled": authentik_enabled(),
            "eid_enabled": eid_enabled(),
            "authentik_button_label": _button_label(),
            "is_demo": False,
        },
        status_code=code,
    )


def _sign_state_cookie(state: str, verifier: str, expires_at: int) -> str:
    secret = settings.secret_key.encode()
    payload = f"{state}|{verifier}|{expires_at}".encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{state}|{verifier}|{expires_at}|{sig_b64}"


def _verify_state_cookie(
    cookie_val: str | None, expected_state: str
) -> str | None:
    """Return the PKCE verifier if the cookie is valid, else None."""
    if not cookie_val or not expected_state:
        return None
    parts = cookie_val.split("|")
    if len(parts) != 4:
        return None
    state, verifier, exp_str, sig_b64 = parts
    if not hmac.compare_digest(state, expected_state):
        return None
    try:
        expires_at = int(exp_str)
    except ValueError:
        return None
    if expires_at < int(time.time()):
        return None
    secret = settings.secret_key.encode()
    payload = f"{state}|{verifier}|{expires_at}".encode()
    expected_sig = hmac.new(secret, payload, hashlib.sha256).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()
    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        return None
    return verifier


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) — RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@router.get("/auth/authentik/login")
async def login(request: Request):
    if not authentik_enabled():
        return _login_error(request, "Authentik login is not configured", 503)

    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    redirect_uri = f"{_public_base_url()}/auth/authentik/callback"

    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    import urllib.parse

    target = f"{_authorize_url()}?{urllib.parse.urlencode(params)}"

    expires_at = int(time.time()) + _STATE_TTL_SECONDS
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        _STATE_COOKIE,
        _sign_state_cookie(state, verifier, expires_at),
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # TLS terminated upstream; cookie travels first-party
        path="/",
    )
    return response


@router.get("/auth/authentik/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if not authentik_enabled():
        return _login_error(request, "Authentik login is not configured", 503)
    if error:
        log.warning("authentik callback error: %s — %s", error, error_description)
        return _login_error(
            request,
            f"Login failed at Authentik ({error})",
            400,
        )
    if not code or not state:
        return _login_error(request, "Missing OIDC parameters", 400)

    verifier = _verify_state_cookie(request.cookies.get(_STATE_COOKIE), state)
    if verifier is None:
        return _login_error(
            request, "Login expired — please retry from the login page", 400
        )

    redirect_uri = f"{_public_base_url()}/auth/authentik/callback"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            tok_resp = await client.post(
                _token_url(),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": _client_id(),
                    "code_verifier": verifier,
                },
                auth=(_client_id(), _client_secret()),
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError:
            log.exception("authentik token endpoint unreachable")
            return _login_error(request, "Login failed — Authentik unreachable", 502)
        if not tok_resp.is_success:
            log.warning(
                "authentik token exchange failed status=%s body=%s",
                tok_resp.status_code,
                tok_resp.text[:300],
            )
            return _login_error(
                request,
                f"Login failed (token status {tok_resp.status_code})",
                502,
            )
        access_token = tok_resp.json().get("access_token")
        if not access_token:
            return _login_error(request, "Login failed — no access token returned", 502)

        try:
            ui_resp = await client.get(
                _userinfo_url(),
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.RequestError:
            log.exception("authentik userinfo endpoint unreachable")
            return _login_error(
                request, "Login failed — Authentik userinfo unreachable", 502
            )
        if not ui_resp.is_success:
            return _login_error(
                request,
                f"Login failed (userinfo status {ui_resp.status_code})",
                502,
            )
        userinfo = ui_resp.json()

        sub = str(userinfo.get("sub") or "")
        email = (userinfo.get("email") or "").strip().lower()
        display_name = (
            userinfo.get("name")
            or userinfo.get("preferred_username")
            or userinfo.get("nickname")
            or None
        )

        if not sub or "@" not in email:
            return _login_error(
                request, "Authentik did not return a usable identity", 400
            )

        handoff_secret = os.environ.get("SAEBOOKS_OAUTH_HANDOFF_SECRET", "")
        if not handoff_secret:
            return _login_error(
                request, "Server not configured (handoff secret missing)", 500
            )

        try:
            handoff = await client.post(
                f"{settings.api_url}/api/v1/auth/oauth-handoff",
                json={
                    "provider": "authentik",
                    "provider_user_id": sub,
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
        request.session.pop("active_company_id", None)
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
    response.delete_cookie(_STATE_COOKIE, path="/")
    return response
