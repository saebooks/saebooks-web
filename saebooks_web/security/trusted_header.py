"""Trusted-header SSO — auto-mint sessions from Authentik forward-auth headers.

When the SAE Books web container sits behind a trusted reverse proxy (Caddy on
OPNsense, with Authentik's outpost performing forward-auth), Authentik injects
``X-authentik-username``, ``X-authentik-email``, ``X-authentik-uid`` into the
request before it reaches uvicorn. This middleware turns those headers into a
SAE Books session by calling the API's existing ``/api/v1/auth/oauth-handoff``
endpoint with provider="authentik". The result: the user lands on the
dashboard immediately after Authentik auth, with no in-app email/password
prompt.

Trust model
-----------
The middleware activates only when ``SAEBOOKS_WEB_TRUSTED_HEADERS=1``. The
container must not be reachable except via the trusted proxy — on this stack
the API/web bind to ``10.0.2.1:18303`` / ``10.0.2.1:18313`` and only Caddy
on the same host talks to them, so end-users cannot forge the headers.

Default-deny
------------
When trusted-header mode is ON, a request that is not already authenticated
and does not carry valid trusted headers MUST be denied — never silently
allowed to fall through to the in-app email/password login form. Allowing
fall-through would let a request that bypassed the trusted proxy reach the
in-app login, defeating the purpose of forward-auth.

The skip list (``_SKIP_PREFIXES``) is the only way to reach an unauthenticated
handler in trusted-header mode: static assets, health checks, favicon, and
``/logout`` (which must remain reachable so a confused session can be cleared).

Failure modes:

- Headers missing on a non-skip path → 401 (proxy misconfigured or bypassed)
- ``SAEBOOKS_OAUTH_HANDOFF_SECRET`` unset → 503 (deployment misconfigured)
- ``oauth-handoff`` returns non-2xx → 502 (upstream auth broken)
- transport error talking to API → 502
"""
from __future__ import annotations

import logging
import os

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from saebooks_web.config import settings

_log = logging.getLogger("saebooks_web.trusted_header")

_SKIP_PREFIXES = ("/static/", "/healthz", "/favicon.ico", "/logout")


def _enabled() -> bool:
    return os.environ.get("SAEBOOKS_WEB_TRUSTED_HEADERS", "0").lower() in (
        "1", "true", "yes",
    )


def _staff_allowlist() -> frozenset[str]:
    raw = os.environ.get("SAE_STAFF_USERNAMES", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _deny(status: int, reason: str, *, path: str) -> PlainTextResponse:
    _log.warning("trusted-header deny %s on %s: %s", status, path, reason)
    return PlainTextResponse(
        f"Forbidden: {reason}\n",
        status_code=status,
        headers={"X-SAE-Auth-Deny": reason},
    )


class TrustedHeaderAuthMiddleware(BaseHTTPMiddleware):
    """Mint a session from Authentik forward-auth headers; deny otherwise."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _enabled():
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Already authenticated — nothing to do.
        if request.session.get("api_token"):
            return await call_next(request)

        email = (request.headers.get("x-authentik-email") or "").strip().lower()
        username = (request.headers.get("x-authentik-username") or "").strip()
        uid = (request.headers.get("x-authentik-uid") or username or "").strip()
        if not email or not uid:
            return _deny(401, "missing trusted-auth headers", path=path)

        secret = os.environ.get("SAEBOOKS_OAUTH_HANDOFF_SECRET", "")
        if not secret:
            return _deny(
                503, "trusted-header config error (handoff secret unset)",
                path=path,
            )

        display_name = (
            request.headers.get("x-authentik-name")
            or username
            or email
        )

        try:
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=5.0,
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/oauth-handoff",
                    headers={"X-OAuth-Handoff-Secret": secret},
                    json={
                        "provider": "authentik",
                        "provider_user_id": uid,
                        "email": email,
                        "display_name": display_name,
                    },
                )
                if not resp.is_success:
                    _log.warning(
                        "oauth-handoff %s for %s: %s",
                        resp.status_code, email, resp.text[:200],
                    )
                    return _deny(
                        502, f"upstream auth handoff failed ({resp.status_code})",
                        path=path,
                    )

                token = resp.json()["access_token"]
                request.session.pop("csrf_token", None)
                request.session["api_token"] = token

                me = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError as exc:
            _log.warning("oauth-handoff transport error: %s", exc)
            return _deny(502, "auth upstream unreachable", path=path)

        if me.is_success:
            prof = me.json()
            request.session["username"] = (
                prof.get("name")
                or prof.get("username")
                or prof.get("email")
                or username
            )
            request.session["user_role"] = prof.get("role", "")
            allow = _staff_allowlist()
            u = (prof.get("username") or "").lower()
            e = (prof.get("email") or email).lower()
            request.session["is_sae_staff"] = bool(
                allow and (u in allow or e in allow)
            )
        else:
            request.session["is_sae_staff"] = False
            request.session["user_role"] = ""

        return await call_next(request)
