"""POST /set-locale — language-switcher endpoint (EE GUI prep, Packet 2b).

Writes the user's explicit language choice to ``request.session`` (the
negotiation order's top priority per ``saebooks_web.i18n.middleware``:
session/cookie override -> Accept-Language -> jurisdiction default) and
also mirrors it into the ``saebooks_locale`` cookie so the choice survives
for anonymous/pre-login pages (e.g. the login screen itself) where a
server session may not carry it durably across a login that rotates the
session.

Standard urlencoded POST -> protected by CSRFMiddleware like every other
state-changing form in this app (see security/csrf.py); the template
includes ``{{ csrf_input(request) }}`` exactly like every other POST form
here. Anonymous sessions (no ``api_token``) are exempt from the CSRF check
by that middleware's own design (no logged-in identity to forge against),
so the switcher also works pre-login.
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from saebooks_web.i18n import SUPPORTED_LOCALES, normalize_locale
from saebooks_web.i18n.middleware import COOKIE_NAME, SESSION_LOCALE_KEY

router = APIRouter()

#: One year — a language preference is durable, not session-length.
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _safe_next(next_value: str | None) -> str:
    """Same-origin-only redirect target; default '/' on anything else."""
    if not next_value:
        return "/"
    parsed = urlparse(next_value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return next_value if next_value.startswith("/") else "/"


@router.post("/set-locale", include_in_schema=False)
async def set_locale(request: Request) -> RedirectResponse:
    form = await request.form()
    requested = str(form.get("locale") or "")
    redirect_to = _safe_next(str(form.get("next") or request.headers.get("referer") or "/"))

    # Reject unsupported locales silently (no-op redirect) rather than 400 —
    # a stale/tampered form value shouldn't break navigation.
    if requested not in SUPPORTED_LOCALES:
        return RedirectResponse(url=redirect_to, status_code=303)

    locale = normalize_locale(requested)
    if "session" in request.scope:
        request.session[SESSION_LOCALE_KEY] = locale

    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        locale,
        max_age=_COOKIE_MAX_AGE,
        samesite="lax",
        httponly=True,  # server-read only; no client JS needs this value
    )
    return response


__all__ = ["router"]
