"""Estonian eID login routes — the web surface over ``eid_providers``.

Mirrors the sibling provider modules (``webauthn_sso``, ``authentik_sso``):
one APIRouter, feature detection via a cheap ``eid_enabled()`` the login
page calls, all state in the signed session cookie, engine handoff to mint
the API JWT.

Gating (paid tier only)
-----------------------
SK ID Solutions bills production authentications per transaction, so eID
is a paid-tier feature: every route 404s unless the ``eid_auth`` feature
flag is active for the running ``SAEBOOKS_EDITION`` (see ``features.py``
— business/pro/enterprise/developer; free/community never sees it) AND
the deployment is EE-branded (``SAEBOOKS_BRAND=tasur``) or explicitly
opted in via ``SAEBOOKS_EID_UI=1``. The engine-side licence flag
(``FLAG_EID_AUTH``) is named engine-lane work.

Flow
----
* ``GET  /auth/eid/login``      — provider picker page (Smart-ID / Mobiil-ID)
* ``POST /auth/eid/start``      — start auth; returns the verification-code
                                  fragment which then polls…
* ``POST /auth/eid/poll``       — …until SK completes; on success either
                                  logs in (login mode) or stores the link
                                  (link mode, settings page)
* ``GET  /settings/eid``        — link management (authenticated)
* ``POST /settings/eid/unlink`` — remove the current user's link

Login requires a pre-existing link created from settings by an
authenticated user: an eID assertion whose personal code is not linked is
refused. Self-serve signup via eID is a business decision, deliberately
not built.

Privacy: personal codes never appear in logs; the in-flight code lives
only in the signed session cookie (the user's own browser) for the
duration of the ceremony and is scrubbed on completion.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web import eid_links
from saebooks_web.brand import current_brand
from saebooks_web.config import settings
from saebooks_web.eid_providers import (
    EidError,
    EidLiveCredentialsMissing,
    enabled_provider_keys,
    get_provider,
    normalize_personal_code,
)
from saebooks_web.features import is_feature_enabled
from saebooks_web.i18n import current_locale, gettext as _
from saebooks_web.security.csrf import ensure_csrf_token

logger = logging.getLogger("saebooks_web.eid_sso")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_SESSION_KEY = "eid_auth"
_FLOW_MAX_AGE_SECONDS = 300  # SK sessions expire well within this


def eid_enabled() -> bool:
    """Paid-tier flag AND jurisdiction-appropriate deployment."""
    if not is_feature_enabled("eid_auth"):
        return False
    if os.environ.get("SAEBOOKS_EID_UI", "").strip().lower() in ("1", "true", "yes"):
        return True
    return current_brand().key == "tasur"


def _require_enabled() -> None:
    if not eid_enabled():
        # Same behaviour as the webauthn routes when the feature is off.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "eid_disabled")


def _error_message(exc: EidError) -> str:
    """Translate a provider error code into a user-facing message."""
    messages = {
        "user_refused": _("You declined the sign-in request on your device."),
        "timeout": _("The request expired before it was confirmed. Please try again."),
        "wrong_vc": _("The wrong verification code was chosen. Please try again."),
        "no_account": _("No active Smart-ID or Mobiil-ID account was found for these details."),
        "unavailable": _("The eID service is temporarily unavailable. Please try again shortly."),
        "validation_failed": _("The authentication response could not be verified."),
        "live_credentials_missing": _("eID sign-in is not configured on this server."),
        "eid_error": _("eID sign-in failed. Please try again."),
    }
    return messages.get(exc.code, messages["eid_error"])


def _fragment(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)


def _error_fragment(request: Request, message: str, *, mode: str, status_code: int = 200) -> HTMLResponse:
    """Render the retryable error fragment (200 so HTMX swaps it in)."""
    return _fragment(
        request,
        "auth/_eid_error.html",
        {"message": message, "mode": mode},
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/auth/eid/login", response_class=HTMLResponse, response_model=None)
async def eid_login_page(request: Request) -> HTMLResponse | RedirectResponse:
    _require_enabled()
    if request.session.get("api_token"):
        return RedirectResponse(url="/", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/eid_login.html",
        {"providers": enabled_provider_keys(), "mode": "login"},
    )


@router.get("/settings/eid", response_class=HTMLResponse, response_model=None)
async def eid_settings_page(request: Request) -> HTMLResponse | RedirectResponse:
    _require_enabled()
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    email = await _session_email(request)
    link = eid_links.find_link_for_email(email) if email else None
    return _TEMPLATES.TemplateResponse(
        request,
        "settings/eid.html",
        {"providers": enabled_provider_keys(), "mode": "link", "link": link},
    )


# ---------------------------------------------------------------------------
# Flow — start + poll (shared by login and link modes)
# ---------------------------------------------------------------------------


@router.post("/auth/eid/start", response_model=None)
async def eid_start(
    request: Request,
    provider: str = Form(...),
    personal_code: str = Form(...),
    phone_number: str = Form(""),
    mode: str = Form("login"),
) -> HTMLResponse:
    _require_enabled()
    if mode not in ("login", "link"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad_mode")
    if mode == "link" and not request.session.get("api_token"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")

    code = normalize_personal_code(personal_code)
    if code is None:
        return _error_fragment(
            request, _("That does not look like a valid Estonian personal code."), mode=mode
        )
    try:
        prov = get_provider(provider)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_provider") from None

    try:
        start = await prov.start_authentication(
            code, phone_number=phone_number or None, language=current_locale.get()
        )
    except EidLiveCredentialsMissing as exc:
        logger.error("eid start refused: %s", exc.code)
        return _error_fragment(request, _error_message(exc), mode=mode)
    except EidError as exc:
        logger.info("eid start failed: provider=%s code=%s", provider, exc.code)
        return _error_fragment(request, _error_message(exc), mode=mode)

    request.session[_SESSION_KEY] = {
        "mode": mode,
        "state": start.state,
        "vc": start.verification_code,
        "started_at": int(time.time()),
    }
    return _fragment(
        request,
        "auth/_eid_pending.html",
        {
            "verification_code": start.verification_code,
            "mode": mode,
            "csrf_token": ensure_csrf_token(request.session),
        },
    )


@router.post("/auth/eid/poll", response_model=None)
async def eid_poll(request: Request) -> Response:
    _require_enabled()
    flow = request.session.get(_SESSION_KEY)
    if not flow or not isinstance(flow, dict) or "state" not in flow:
        return _error_fragment(
            request, _("This sign-in attempt has expired. Please start again."), mode="login"
        )
    mode = flow.get("mode", "login")
    if int(time.time()) - int(flow.get("started_at", 0)) > _FLOW_MAX_AGE_SECONDS:
        request.session.pop(_SESSION_KEY, None)
        return _error_fragment(
            request,
            _("The request expired before it was confirmed. Please try again."),
            mode=mode,
        )

    state = flow["state"]
    try:
        prov = get_provider(state.get("provider", ""))
    except KeyError:
        request.session.pop(_SESSION_KEY, None)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_provider") from None

    try:
        assertion = await prov.check_session(state)
    except EidError as exc:
        request.session.pop(_SESSION_KEY, None)
        logger.info("eid flow ended: provider=%s code=%s", prov.key, exc.code)
        return _error_fragment(request, _error_message(exc), mode=mode)

    if assertion is None:
        # Still waiting on the user — re-render the verification-code
        # fragment (it re-arms its own delayed poll).
        return _fragment(
            request,
            "auth/_eid_pending.html",
            {
                "verification_code": flow.get("vc", ""),
                "mode": mode,
                "csrf_token": ensure_csrf_token(request.session),
            },
        )

    # Ceremony complete and cryptographically validated. Scrub flow state
    # (it holds the in-flight personal code) before doing anything else.
    request.session.pop(_SESSION_KEY, None)

    if mode == "link":
        return await _complete_link(request, assertion)
    return await _complete_login(request, assertion)


# ---------------------------------------------------------------------------
# Completion — link mode
# ---------------------------------------------------------------------------


async def _complete_link(request: Request, assertion) -> HTMLResponse:
    if not request.session.get("api_token"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")
    email = await _session_email(request)
    if not email:
        return _error_fragment(
            request, _("Could not resolve your account email. Please re-login and retry."),
            mode="link",
        )
    try:
        record = eid_links.link(email, assertion.personal_code, assertion.provider)
    except ValueError:
        return _error_fragment(
            request,
            _("This personal code is already linked to a different account."),
            mode="link",
        )
    logger.info("eid link created: provider=%s", assertion.provider)
    return _fragment(
        request,
        "auth/_eid_link_success.html",
        {"link": record, "display_name": assertion.display_name},
    )


@router.post("/settings/eid/unlink", response_model=None)
async def eid_unlink(request: Request) -> RedirectResponse:
    _require_enabled()
    if not request.session.get("api_token"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")
    email = await _session_email(request)
    if email:
        eid_links.unlink(email)
    return RedirectResponse(url="/settings/eid", status_code=303)


# ---------------------------------------------------------------------------
# Completion — login mode
# ---------------------------------------------------------------------------


async def _complete_login(request: Request, assertion) -> Response:
    record = eid_links.find_link(assertion.personal_code)
    if record is None:
        # Deliberate business rule: no self-serve signup via eID. The code
        # is valid and authenticated, but nobody has claimed it.
        logger.info("eid login refused: no linked account (provider=%s)", assertion.provider)
        return _error_fragment(
            request,
            _(
                "Your identity was verified, but no account is linked to this "
                "personal code yet. Sign in with your password first, then link "
                "your eID under Settings."
            ),
            mode="login",
        )

    token = await _handoff_token(record["email"], assertion)
    if not token:
        return _error_fragment(
            request, _("Sign-in failed on the server. Please try again."), mode="login"
        )

    # Mint the saebooks-web session — same sequence as the sibling flows:
    # rotate CSRF, drop company context, store the JWT, hydrate profile.
    request.session.pop("csrf_token", None)
    request.session.pop("active_company_id", None)
    request.session["api_token"] = token
    await _hydrate_profile(request, token)
    logger.info("eid login ok: provider=%s", assertion.provider)
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = "/"
    return response


async def _handoff_token(email: str, assertion) -> str | None:
    """Exchange the validated assertion for an engine JWT via oauth-handoff.

    Engine-lane dependency: ``/api/v1/auth/oauth-handoff`` must accept
    ``provider="eid"`` (today it allows discourse/authentik/cf-access
    only). Until that lands this returns None and the UI shows a clean
    failure — nothing crashes.
    """
    handoff_secret = os.environ.get("SAEBOOKS_OAUTH_HANDOFF_SECRET", "")
    if not handoff_secret:
        logger.error("eid login failed: SAEBOOKS_OAUTH_HANDOFF_SECRET not configured")
        return None
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=15.0) as client:
            resp = await client.post(
                "/api/v1/auth/oauth-handoff",
                json={
                    "provider": "eid",
                    "provider_user_id": f"PNOEE-{assertion.personal_code}",
                    "email": email,
                    "display_name": assertion.display_name or None,
                },
                headers={"X-OAuth-Handoff-Secret": handoff_secret},
            )
    except httpx.RequestError:
        logger.warning("eid handoff unreachable")
        return None
    if not resp.is_success:
        logger.warning("eid handoff failed: HTTP %s", resp.status_code)
        return None
    return resp.json().get("access_token") or None


async def _hydrate_profile(request: Request, token: str) -> None:
    """Fetch /auth/me and populate the session — mirrors webauthn_sso."""
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=5.0) as client:
            me = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.RequestError:
        request.session["is_sae_staff"] = False
        request.session["user_role"] = ""
        return
    if not me.is_success:
        request.session["is_sae_staff"] = False
        request.session["user_role"] = ""
        return
    profile = me.json()
    request.session["username"] = (
        profile.get("name") or profile.get("username") or profile.get("email") or ""
    )
    request.session["user_role"] = profile.get("role", "")
    allow = frozenset(
        s.strip().lower()
        for s in os.environ.get("SAE_STAFF_USERNAMES", "").split(",")
        if s.strip()
    )
    uname = (profile.get("username") or "").lower()
    uemail = (profile.get("email") or "").lower()
    request.session["is_sae_staff"] = bool(allow and (uname in allow or uemail in allow))


async def _session_email(request: Request) -> str:
    """The authenticated user's email, via /auth/me with the session JWT."""
    token = request.session.get("api_token")
    if not token:
        return ""
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=5.0) as client:
            me = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.RequestError:
        return ""
    if not me.is_success:
        return ""
    return (me.json().get("email") or "").strip().lower()
