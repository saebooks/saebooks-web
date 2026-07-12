"""LocaleMiddleware — sets ``saebooks_web.i18n.current_locale`` per request.

Negotiation order (scope decision 2): session/cookie override ->
Accept-Language header -> jurisdiction default (EE -> et, AU -> en).
Durable per-user preference (an engine-lane User model column) is
explicitly deferred — this middleware only does per-request negotiation.

Must run AFTER CompanyContextMiddleware in the dispatch chain (i.e. added
*before* it in source, since Starlette's add_middleware inserts at index 0
and the last add_middleware call ends up outermost — see the ordering
note in main.py) so ``request.state.active_company_jurisdiction`` is
already populated when this middleware reads it for the jurisdiction
fallback.

Sets the contextvar via ``.set()`` and restores the previous value in a
``finally`` block using the returned Token — this keeps the contextvar
correctly scoped even though Starlette's BaseHTTPMiddleware runs
downstream code inside the same coroutine (belt-and-braces on top of the
per-task isolation contextvars already provide; see i18n/__init__.py
concurrency note).
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware

from saebooks_web.i18n import SUPPORTED_LOCALES, current_locale, normalize_locale

#: Session/cookie key for an explicit user override, e.g. a "language"
#: picker in account settings. Read-only in this packet — no UI writes it
#: yet (that's a future packet); wiring is here so that packet is a small
#: add, not a new negotiation path.
SESSION_LOCALE_KEY = "locale"
COOKIE_NAME = "saebooks_locale"

#: Jurisdiction -> default locale. Mirrors company_context.py's
#: active_company_jurisdiction codes ("AU"/"EE"/...). Unknown/missing
#: jurisdiction falls through to DEFAULT_LOCALE (en).
_JURISDICTION_DEFAULT_LOCALE: dict[str, str] = {
    "EE": "et",
    "AU": "en",
}


def _locale_from_accept_language(header_value: str | None) -> str | None:
    """Parse the first acceptable, supported language from Accept-Language.

    Minimal q-value-aware parse: split on comma, strip ";q=..." weighting
    (order in the header is already client-preference order for the
    common case; a full q-sort is unnecessary complexity for a 3-locale
    app). Returns None if nothing in the header maps to a supported
    locale, so the caller can fall through to the jurisdiction default.
    """
    if not header_value:
        return None
    for part in header_value.split(","):
        tag = part.split(";", 1)[0].strip()
        normalized = normalize_locale(tag)
        # normalize_locale silently defaults unknown tags to "en" — only
        # trust that default if "en"/"en-*" was actually what was sent.
        if normalized in SUPPORTED_LOCALES and (
            normalized != "en" or tag.lower().split("-")[0] == "en"
        ):
            return normalized
    return None


def resolve_locale(request) -> str:
    """Pure negotiation logic, exposed separately from dispatch for tests."""
    try:
        session_value = request.session.get(SESSION_LOCALE_KEY) if "session" in request.scope else None
    except Exception:
        session_value = None
    if session_value:
        return normalize_locale(session_value)

    cookie_value = request.cookies.get(COOKIE_NAME)
    if cookie_value:
        return normalize_locale(cookie_value)

    header_locale = _locale_from_accept_language(request.headers.get("accept-language"))
    if header_locale:
        return header_locale

    jurisdiction = getattr(request.state, "active_company_jurisdiction", None)
    if jurisdiction:
        mapped = _JURISDICTION_DEFAULT_LOCALE.get(jurisdiction.upper())
        if mapped:
            return mapped

    return normalize_locale(None)


class LocaleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        locale = resolve_locale(request)
        token = current_locale.set(locale)
        request.state.active_locale = locale
        try:
            return await call_next(request)
        finally:
            current_locale.reset(token)


__all__ = ["LocaleMiddleware", "resolve_locale", "SESSION_LOCALE_KEY", "COOKIE_NAME"]
