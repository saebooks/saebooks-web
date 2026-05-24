"""Demo auto-login — bootstrap a session for the public Cashbook demo.

When ``SAEBOOKS_DEMO_AUTOLOGIN_EMAIL`` and ``SAEBOOKS_DEMO_AUTOLOGIN_PASSWORD``
are set, every unauthenticated request to a non-skip path performs a
server-side login against ``/api/v1/auth/login`` using those credentials and
stuffs the resulting token + profile into the signed session cookie. The
end-user never sees the login screen.

This is intentionally NOT gated by network position — it is opt-in via env
var, only enabled on the cashbook-demo container. Any host that flips this
on becomes a public demo where the configured account is automatically
shared. NEVER set these env vars on the main app.saebooks.com.au stack.

Visitors land on / and (in cashbook-mode demos) get redirected to /cashbook
so the demo lands on the relevant surface immediately.
"""
from __future__ import annotations

import logging
import os

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from saebooks_web.config import settings

_log = logging.getLogger("saebooks_web.demo_autologin")

# Anything matching these prefixes bypasses autologin entirely (static
# assets, health, the login routes themselves so manual login still
# works as an escape hatch, logout, demo-marker pages).
_SKIP_PREFIXES = (
    "/static/",
    "/healthz",
    "/favicon.ico",
    "/login",
    "/logout",
    "/oauth/",
)


def _email() -> str:
    return os.environ.get("SAEBOOKS_DEMO_AUTOLOGIN_EMAIL", "").strip()


def _password() -> str:
    return os.environ.get("SAEBOOKS_DEMO_AUTOLOGIN_PASSWORD", "")


def _land_path() -> str:
    """Where to redirect after autologin when path is /.

    Defaults to /cashbook for the cashbook demo. Override via
    SAEBOOKS_DEMO_LAND_PATH for other demo flavours later.
    """
    return os.environ.get("SAEBOOKS_DEMO_LAND_PATH", "/cashbook").strip() or "/"


def _enabled() -> bool:
    return bool(_email()) and bool(_password())


class DemoAutoLoginMiddleware(BaseHTTPMiddleware):
    """If the demo creds env vars are set, mint a session on the fly."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _enabled():
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Already authenticated — verify the token still works before trusting.
        # Stale cookies (e.g. from a wiped demo DB) carry a token for a user
        # that no longer exists; trusting them produces a redirect loop because
        # every downstream API call 401s. Verify via /auth/me, drop on failure.
        existing_token = request.session.get("api_token")
        if existing_token:
            try:
                async with httpx.AsyncClient(
                    base_url=settings.api_url, timeout=8.0
                ) as client:
                    me_resp = await client.get(
                        "/api/v1/auth/me",
                        headers={"Authorization": f"Bearer {existing_token}"},
                    )
            except httpx.RequestError as exc:
                _log.warning("demo autologin verify error: %r", exc)
                return await call_next(request)
            if me_resp.is_success:
                if path == "/":
                    land = _land_path()
                    if land != "/":
                        return RedirectResponse(land, status_code=303)
                return await call_next(request)
            _log.info(
                "demo autologin: dropping stale token (auth/me %s)",
                me_resp.status_code,
            )
            request.session.pop("api_token", None)
            request.session.pop("username", None)
            request.session.pop("user_role", None)

        # Server-side login.
        email = _email()
        password = _password()
        try:
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=8.0
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/login",
                    json={"email": email, "password": password},
                )
                if not resp.is_success:
                    _log.warning(
                        "demo autologin failed (%s): %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return await call_next(request)
                token = resp.json()["access_token"]
                me_resp = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError as exc:
            _log.warning("demo autologin transport error: %r", exc)
            return await call_next(request)

        request.session.pop("csrf_token", None)
        request.session.pop("active_company_id", None)
        request.session["api_token"] = token

        if me_resp.is_success:
            profile = me_resp.json()
            request.session["username"] = (
                profile.get("name")
                or profile.get("username")
                or profile.get("email")
                or ""
            )
            request.session["user_role"] = profile.get("role", "")

        # Send / → /cashbook (or whatever SAEBOOKS_DEMO_LAND_PATH says).
        if path == "/":
            land = _land_path()
            if land != "/":
                return RedirectResponse(land, status_code=303)

        return await call_next(request)
