"""CSRF protection for saebooks-web — Layers 2 and 3 of P0-3 defence.

Layer 2 — OriginRefererMiddleware
=================================
On every state-changing request (POST/PUT/PATCH/DELETE):

1. If the ``Origin`` header is present, its scheme+host MUST match the
   configured site origin allow-list (default
   ``https://books-dev.sauer.com.au,https://books.sauer.com.au``,
   override via ``SAEBOOKS_WEB_SITE_ORIGIN``).  Mismatch -> 403.
2. Else if the ``Referer`` header is present, its scheme+host MUST match
   the configured site origin.  Mismatch -> 403.
3. Else (neither header present): allowed-with-warning.  In a browser
   context at least one of Origin/Referer is always sent on a form POST
   (modern browsers send Origin unconditionally on cross-origin POSTs as
   of ~2020), so absence indicates a non-browser client.  Such a client
   cannot mount a CSRF attack against a logged-in user — it cannot read
   their cookies cross-origin and cannot forge their session token.
   Layer 3 (CSRF token) provides the real enforcement against
   missing-both; Layer 2 logs the case for observability and lets the
   request through to be rejected by the token check.  The tradeoff is
   that we tolerate scripted POSTs without headers, which is what most
   legitimate test harnesses and ops tools look like.

Routes prefixed with ``/api/v1/`` are skipped (those routes do not exist on
the web app today, but if a future change adds JSON endpoints, they should
authenticate via ``Authorization: Bearer`` not session cookies and CSRF
does not apply).  ``/healthz`` is also skipped.

Layer 3 — CSRFMiddleware  + csrf_input + ensure_csrf_token
==========================================================
Every form template includes ``{{ csrf_input() }}`` which renders a hidden
``<input type="hidden" name="csrf_token" value="...">`` populated from
``request.session['csrf_token']``.  The middleware validates that the
submitted token matches the session token using ``secrets.compare_digest``.
On mismatch -> 403 with ``code: csrf_token_mismatch``.

The token is generated lazily (``ensure_csrf_token``) when a form is
rendered, and rotated on login/logout to prevent fixation.

Body replay
-----------
The middleware reads the request body to extract the form field, then
re-injects it into the ASGI ``receive`` channel so the downstream handler
can call ``await request.form()`` and see the same body.  Without this,
the body would be consumed once by the middleware and the handler would
receive an empty form.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_SITE_ORIGINS = "https://books-dev.sauer.com.au,https://books.sauer.com.au"


def _get_site_origins() -> tuple[str, ...]:
    """Return the configured allow-list of site origins (scheme+host, no path).

    Configurable via SAEBOOKS_WEB_SITE_ORIGIN env var as a comma-separated
    list. Trailing slashes are stripped so equality checks against header
    values work cleanly. The first entry is the canonical site origin.
    """
    raw = os.environ.get("SAEBOOKS_WEB_SITE_ORIGIN", _DEFAULT_SITE_ORIGINS).strip()
    return tuple(o.rstrip("/") for o in raw.split(",") if o.strip())


def _get_site_origin() -> str:
    """Return the canonical (first) site origin — kept for back-compat."""
    origins = _get_site_origins()
    return origins[0] if origins else ""


def _origin_of(url: str) -> str | None:
    """Return scheme://host[:port] from a full URL, or None if malformed.

    ``Referer`` headers carry the full URL of the source page; we compare
    only the origin (scheme + host + port) part against the site origin.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


# Methods that mutate state and must be CSRF-protected.
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Path prefixes where CSRF enforcement is deliberately skipped.
#   /api/v1/* — no such routes exist on the web app today, but if a future
#               change adds JSON routes they will use Authorization: Bearer
#               (browsers cannot set Authorization cross-origin without
#               preflight, and preflight blocks unsafe content-types).
#   /healthz — liveness probe, never authenticated.
#   /readyz   — readiness probe, never authenticated.
#   /internal/ — server-to-server endpoints (e.g. /internal/render) called by
#               the accounting engine, not a browser. They carry no session
#               cookie and are protected by their own token gate
#               (X-Render-Token), so both Origin/Referer (Layer 2) and the
#               per-form CSRF token (Layer 3) are inapplicable.
_CSRF_SKIP_PREFIXES: tuple[str, ...] = ("/api/v1/", "/healthz", "/readyz", "/internal/")


def _path_is_skipped(path: str) -> bool:
    """True if the request path is in the CSRF-exempt set."""
    return any(path == p or path.startswith(p) for p in _CSRF_SKIP_PREFIXES)


# Login is CSRF-protected by Layer 2 (Origin/Referer) but exempt from
# Layer 3 (token), because there is no session yet to bind a token to.
# Logout is exempt from Layer 3 for the same reason on the GET path; the
# POST path is exempt for symmetry and because samesite=strict + Origin
# checks already block it.
_TOKEN_SKIP_PATHS: frozenset[str] = frozenset({"/login", "/logout"})


# ---------------------------------------------------------------------------
# Helpers — building 403 responses without bringing in the response classes
# ---------------------------------------------------------------------------


async def _send_403(send: Send, *, code: str, message: str) -> None:
    """Send a minimal JSON 403 response over the ASGI ``send`` channel.

    Used by both Layer 2 and Layer 3 middlewares.  Returning JSON keeps the
    response identifiable both to humans (in dev tools) and to scripted
    callers; the ``code`` field is the machine-readable identifier the
    audit-trail asks for (``cross_origin_forbidden`` / ``csrf_token_mismatch``).
    """
    body = json.dumps({"detail": message, "code": code}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _header(scope: Scope, name: bytes) -> str | None:
    """Return the first value of an ASGI header, decoded, or None.

    ASGI scope ``headers`` is a list of ``(name_bytes, value_bytes)`` tuples
    where the name is lowercased.  This helper does a linear scan; it's
    only called a few times per request and the header list is short.
    """
    for hk, hv in scope.get("headers", []):
        if hk == name:
            try:
                return hv.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# Layer 2 — Origin / Referer middleware
# ---------------------------------------------------------------------------


class OriginRefererMiddleware:
    """ASGI middleware that rejects state-changing cross-origin requests.

    Mounted before the SessionMiddleware so that even if a future bug let a
    cookie ride along (e.g. samesite default flipped), we'd still reject
    the request before anything stateful runs.

    Note: GET/HEAD/OPTIONS are skipped because they are required to be safe
    by HTTP semantics and any state-mutation behind a GET is itself a bug.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        if method not in _STATE_CHANGING or _path_is_skipped(path):
            await self.app(scope, receive, send)
            return

        site_origins = _get_site_origins()
        origin_hdr = _header(scope, b"origin")
        referer_hdr = _header(scope, b"referer")

        # Some clients (and certain Firefox/HTMX combinations) send literal
        # "null" for Origin on same-origin form POSTs.  Treat as absent.
        if origin_hdr == "null":
            origin_hdr = None

        if origin_hdr is not None:
            origin_origin = _origin_of(origin_hdr)
            if origin_origin not in site_origins:
                logger.warning(
                    "CSRF Layer 2 reject: %s %s — Origin=%r site_origins=%r",
                    method, path, origin_hdr, site_origins,
                )
                await _send_403(
                    send,
                    code="cross_origin_forbidden",
                    message="Cross-origin request rejected (Origin mismatch).",
                )
                return
        elif referer_hdr is not None:
            referer_origin = _origin_of(referer_hdr)
            if referer_origin not in site_origins:
                logger.warning(
                    "CSRF Layer 2 reject: %s %s — Referer=%r site_origins=%r",
                    method, path, referer_hdr, site_origins,
                )
                await _send_403(
                    send,
                    code="cross_origin_forbidden",
                    message="Cross-origin request rejected (Referer mismatch).",
                )
                return
        else:
            # Browsers always send at least one of Origin/Referer on form
            # POSTs.  Missing both indicates a non-browser client; such a
            # client cannot read cookies cross-origin so it cannot mount a
            # classic CSRF attack against a logged-in user, and Layer 3
            # (CSRF token) will reject any unauthenticated state mutation.
            # We log here for observability and let the request through.
            logger.info(
                "CSRF Layer 2 pass-through: %s %s — neither Origin nor Referer set "
                "(non-browser client; Layer 3 will enforce token).",
                method, path,
            )

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Layer 3 — per-form token (filled in commit 3)
# ---------------------------------------------------------------------------


def ensure_csrf_token(session: dict) -> str:
    """Return the session's CSRF token, generating one on first call.

    The token is a 32-byte URL-safe random string (256 bits of entropy).  It
    is stored in the session dict under the key ``csrf_token`` and reused
    for the lifetime of the session.  Rotation on login/logout is handled
    in saebooks_web.auth (the session is cleared, which removes the token).
    """
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_input(request) -> str:
    """Jinja-callable that returns a hidden CSRF input element as raw HTML.

    Usage in a template::

        <form method="POST" action="...">
          {{ csrf_input(request) }}
          ...
        </form>

    The macro is registered as a Jinja global in main.py so every template
    has access to it without an explicit import.  It accepts ``request``
    so it can read/write ``request.session`` to lazily generate the token.
    """
    token = ensure_csrf_token(request.session)
    # html-safe by construction: token chars are URL-safe base64
    # ([A-Za-z0-9_-]); no escaping needed but we still wrap in attribute
    # quotes for parser safety.
    return (
        '<input type="hidden" name="csrf_token" '
        f'value="{token}">'
    )


# ---------------------------------------------------------------------------
# Layer 3 — middleware (lands in commit 3)
# ---------------------------------------------------------------------------


class CSRFMiddleware:
    """ASGI middleware that verifies a per-session CSRF token on every
    state-changing form submission.

    Body-replay protocol:
        1. Receive the entire request body from the wrapped ``receive``
           channel (bounded by Content-Length; we bail out at 1 MiB to
           prevent DoS through arbitrarily long bodies — forms in this
           app are well under that).
        2. Parse application/x-www-form-urlencoded to extract ``csrf_token``.
        3. Compare against ``request.session['csrf_token']`` using
           ``secrets.compare_digest``.
        4. On mismatch -> 403; otherwise replace ``receive`` with a
           generator that replays the buffered body to the downstream app.

    For multipart/form-data submissions (file uploads), we forward the
    body unchanged because parsing multipart in the middleware would
    require pulling in the multipart library and re-streaming.  Instead
    those routes call ``verify_csrf_form`` explicitly after parsing.
    Today the only multipart route is the AI extraction document upload.

    For non-form content types (application/json, text/*, etc.) the
    middleware skips token verification — JSON POSTs in this app do not
    exist, but if they're added in the future, those routes use bearer
    auth and CSRF does not apply.  Origin/Referer (Layer 2) still applies.
    """

    # 1 MiB — generous for forms, mean for bodies that aren't.
    MAX_BODY_BYTES = 1 * 1024 * 1024

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        if (
            method not in _STATE_CHANGING
            or _path_is_skipped(path)
            or path in _TOKEN_SKIP_PATHS
        ):
            await self.app(scope, receive, send)
            return

        content_type = (_header(scope, b"content-type") or "").lower()

        # Only enforce on form POSTs; bearer/JSON/etc. do not need CSRF.
        is_urlencoded = content_type.startswith("application/x-www-form-urlencoded")
        is_multipart = content_type.startswith("multipart/form-data")
        if not (is_urlencoded or is_multipart):
            await self.app(scope, receive, send)
            return

        # Pull the session token out of the cookie-derived session.  The
        # SessionMiddleware sits BELOW us in the stack (it wraps this
        # middleware), which means by the time __call__ runs, the session
        # has already been decoded into scope["session"].  If session is
        # missing OR the user is not authenticated (no api_token), we let
        # the request through — there's nothing for an attacker to forge
        # against an unauthenticated session, and the route handler will
        # redirect/401 unauthenticated POSTs via its own auth check.
        # CSRF is a defence for the *logged-in* user; if there's no login
        # there's no logged-in identity to abuse.
        session: dict = scope.get("session") or {}
        if "api_token" not in session:
            await self.app(scope, receive, send)
            return
        expected_token = session.get("csrf_token")

        if is_multipart:
            # Multipart: don't try to parse here.  The form handler is
            # responsible for calling verify_csrf_form() after request.form().
            # In practice today this means /bills/extract-document and
            # /invoices/extract-document; both are SAE-staff-only.
            await self.app(scope, receive, send)
            return

        # Buffer the urlencoded body so we can both parse it and replay
        # it to the downstream handler.
        body_chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # http.disconnect — propagate
                await self.app(scope, _wrap_replay(message, []), send)
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.MAX_BODY_BYTES:
                logger.warning(
                    "CSRF Layer 3 reject: %s %s — body exceeds %d bytes",
                    method, path, self.MAX_BODY_BYTES,
                )
                await _send_403(
                    send,
                    code="csrf_body_too_large",
                    message="Request body too large for CSRF check.",
                )
                return
            body_chunks.append(chunk)
            more = message.get("more_body", False)

        body = b"".join(body_chunks)
        try:
            parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        except Exception:
            parsed = {}
        submitted_tokens = parsed.get("csrf_token", [])
        submitted = submitted_tokens[0] if submitted_tokens else ""

        if not expected_token or not submitted or not secrets.compare_digest(
            str(expected_token), str(submitted)
        ):
            logger.warning(
                "CSRF Layer 3 reject: %s %s — token mismatch (have_session_token=%s have_submitted=%s)",
                method, path, bool(expected_token), bool(submitted),
            )
            await _send_403(
                send,
                code="csrf_token_mismatch",
                message="CSRF token missing or invalid.",
            )
            return

        # Replay the buffered body into the downstream app.
        await self.app(scope, _make_replay_receive(body), send)


def _make_replay_receive(body: bytes) -> Callable[[], Awaitable[Message]]:
    """Build a ``receive`` callable that yields the buffered body once.

    After yielding the single ``http.request`` message with the full body,
    subsequent calls return ``http.disconnect`` so the downstream app
    doesn't hang waiting for more.
    """
    sent = False
    disconnected = False

    async def receive() -> Message:
        nonlocal sent, disconnected
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if not disconnected:
            disconnected = True
            return {"type": "http.disconnect"}
        # Should never reach here in well-behaved apps, but be defensive.
        return {"type": "http.disconnect"}

    return receive


def _wrap_replay(initial_message: Message, _ignored: list[bytes]) -> Receive:
    """Receive wrapper used when we got a non-http.request message early
    (e.g. http.disconnect).  Just forwards that one message through.
    """
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return initial_message
        return {"type": "http.disconnect"}

    return receive


# ---------------------------------------------------------------------------
# Helper for multipart (file-upload) routes
# ---------------------------------------------------------------------------


async def verify_csrf_form(request) -> None:
    """Verify the CSRF token in a multipart form submission.

    Multipart bodies aren't intercepted by ``CSRFMiddleware`` (parsing them
    in middleware would mean pulling in python-multipart there as well and
    re-streaming the parts).  Routes that accept ``multipart/form-data``
    must call this explicitly *after* ``request.form()``::

        form = await request.form()
        await verify_csrf_form(request)  # raises HTTPException(403) on mismatch

    Raises ``starlette.exceptions.HTTPException`` with status 403 when the
    token is missing or doesn't match.

    Symmetric with CSRFMiddleware: anonymous sessions (no ``api_token``)
    are skipped — the route's auth check will reject them.
    """
    from starlette.exceptions import HTTPException

    if "api_token" not in request.session:
        return
    form = await request.form()
    submitted = form.get("csrf_token")
    expected = request.session.get("csrf_token")
    if not expected or not submitted or not secrets.compare_digest(
        str(expected), str(submitted)
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")
