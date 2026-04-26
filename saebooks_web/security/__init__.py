"""Security primitives: CSRF protection (Layers 2 + 3) and helpers.

Layer 1 (samesite=strict) lives in main.py because it is a direct argument
to Starlette's SessionMiddleware.

Layer 2 — origin_referer_middleware:
    Reject state-changing requests whose Origin/Referer does not match the
    configured site origin.  Implemented as a pure ASGI middleware so it can
    short-circuit before any route handler runs and without consuming the
    request body.

Layer 3 — csrf_middleware + csrf_input macro + verify_csrf_token:
    Per-session CSRF token embedded in every form via the {{ csrf_input() }}
    Jinja macro.  An ASGI middleware reads the form body, verifies the token
    matches request.session['csrf_token'], rejects 403 on mismatch, and
    re-injects the body so the downstream handler can read it normally.
"""
from __future__ import annotations

from saebooks_web.security.csrf import (
    csrf_input,
    ensure_csrf_token,
    OriginRefererMiddleware,
    CSRFMiddleware,
)

__all__ = [
    "csrf_input",
    "ensure_csrf_token",
    "OriginRefererMiddleware",
    "CSRFMiddleware",
]
