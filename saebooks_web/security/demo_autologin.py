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
from starlette.responses import RedirectResponse

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


def _ephemeral_enabled() -> bool:
    """Ephemeral per-visit demo mode: provision a fresh, isolated, seeded tenant
    on each root visit via the engine's ``POST /internal/demo/provision`` instead
    of logging into one shared account. Independent of the fixed-cred autologin
    above; when on it takes precedence. Toggled by ``SAEBOOKS_DEMO_EPHEMERAL``."""
    return os.environ.get("SAEBOOKS_DEMO_EPHEMERAL", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _internal_secret() -> str:
    return os.environ.get("DEMO_INTERNAL_SECRET", "").strip()


def _source_ip(request: Request) -> str | None:
    """Best-effort original client IP, forwarded so the engine's per-IP provision
    rate-limit sees the visitor (not the web container)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return request.client.host if request.client is not None else None


class DemoAutoLoginMiddleware(BaseHTTPMiddleware):
    """If the demo creds env vars are set, mint a session on the fly.

    Two modes: the original fixed-cred shared autologin (``_enabled``) and the
    ephemeral per-visit tenant provisioner (``_ephemeral_enabled``, preferred)."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if _ephemeral_enabled():
            return await self._dispatch_ephemeral(request, call_next)
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

    # ------------------------------------------------------------------ #
    # Ephemeral per-visit mode                                            #
    # ------------------------------------------------------------------ #

    async def _token_valid(self, token: str) -> bool:
        """True if the demo JWT still authenticates (its tenant wasn't reaped)."""
        try:
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=8.0
            ) as client:
                r = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return r.is_success
        except httpx.RequestError:
            # Transport hiccup — don't churn a fresh tenant; treat as still valid.
            return True

    async def _provision(self, request: Request) -> dict | None:
        """Mint a fresh ephemeral demo tenant via the engine. Returns the
        provision payload (with a best-effort ``_profile``) or None on
        capacity / rate-limit / guard / transport failure — the caller then
        serves the request unprovisioned rather than 500ing."""
        headers: dict[str, str] = {}
        secret = _internal_secret()
        if secret:
            headers["X-Internal-Secret"] = secret
        ip = _source_ip(request)
        if ip:
            headers["X-Forwarded-For"] = ip
        try:
            # Generous timeout: a fresh tenant + dataset seed (esp. the cashbook
            # flavour, which posts journal entries) can take several seconds.
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=25.0
            ) as client:
                resp = await client.post(
                    "/internal/demo/provision", headers=headers, json={}
                )
                if resp.status_code != 201:
                    _log.warning(
                        "demo provision non-201 (%s): %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None
                data = resp.json()
                if not data.get("access_token"):
                    return None
                profile: dict = {}
                try:
                    me = await client.get(
                        "/api/v1/auth/me",
                        headers={
                            "Authorization": f"Bearer {data['access_token']}"
                        },
                    )
                    if me.is_success:
                        profile = me.json()
                except httpx.RequestError:
                    pass
                data["_profile"] = profile
                return data
        except httpx.RequestError as exc:
            _log.warning("demo provision transport error: %r", exc)
            return None

    async def _dispatch_ephemeral(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        existing = request.session.get("api_token")
        # Fresh tenant on every ROOT visit (Richard's call: opening / refreshing
        # the link == a pristine demo). On deeper paths keep the current tenant
        # while its token still authenticates; re-provision if it was reaped.
        need_fresh = path == "/" or not existing
        if existing and not need_fresh:
            if not await self._token_valid(existing):
                need_fresh = True
                _log.info("demo: token stale (tenant reaped) — reprovisioning")

        if not need_fresh:
            return await call_next(request)

        data = await self._provision(request)
        if data is None:
            # Capacity / rate-limit / transient — let the request through
            # unprovisioned (renders the public landing, no 500).
            return await call_next(request)

        request.session.pop("csrf_token", None)
        request.session.pop("active_company_id", None)
        request.session["api_token"] = data["access_token"]
        profile = data.get("_profile") or {}
        request.session["username"] = (
            profile.get("name")
            or profile.get("username")
            or profile.get("email")
            or data.get("demo_user_email", "")
        )
        request.session["user_role"] = profile.get("role", "")

        if path == "/":
            land = _land_path()
            if land != "/":
                return RedirectResponse(land, status_code=303)
        return await call_next(request)
