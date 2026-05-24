"""CF Access JWT verification — mint a saebooks session from the edge identity.

When ``SAEBOOKS_TRUST_CF_ACCESS=1``, this middleware reads the
``Cf-Access-Jwt-Assertion`` header that Cloudflare Access injects after a user
has authenticated at the edge, verifies its signature against the team's JWKS,
and (if the request is otherwise unauthenticated) calls the existing
``/api/v1/auth/oauth-handoff`` endpoint to mint a local session.

The header is only trusted when the JWT signature is valid against the
configured team's JWKS, the ``aud`` claim matches ``CF_ACCESS_AUD``, and the
``iss`` claim matches ``https://<CF_ACCESS_TEAM_DOMAIN>``. This is what lets us
trust the value even though uvicorn binds to a LAN IP — the JWT is signed by
Cloudflare and we verify the signature on every request.

Config (per-instance .env on the saebooks-web container):

    SAEBOOKS_TRUST_CF_ACCESS=1
    CF_ACCESS_TEAM_DOMAIN=aussieboer61.cloudflareaccess.com
    CF_ACCESS_AUD=<aud-tag-of-the-CF-Access-app>
    SAEBOOKS_OAUTH_HANDOFF_SECRET=<same secret the API expects>

Runs as a starlette middleware INSIDE ``SessionMiddleware`` (so
``request.session`` is available) and ahead of the route handlers. Skips
quietly when not enabled, when the header is missing, or when the user is
already authenticated.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks_web.config import settings

_log = logging.getLogger("saebooks_web.cf_access")

_SKIP_PREFIXES = (
    "/static/", "/healthz", "/favicon.ico", "/logout",
    "/sw.js", "/manifest.webmanifest", "/manifest.json",
)

_JWKS_CACHE: dict[str, tuple[PyJWKClient, float]] = {}
_JWKS_TTL_SECONDS = 3600  # CF rotates rarely; refresh hourly


def _enabled() -> bool:
    return os.environ.get("SAEBOOKS_TRUST_CF_ACCESS", "0").lower() in ("1", "true", "yes")


def _team_domain() -> str | None:
    raw = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
    return raw or None


def _aud() -> str | None:
    raw = os.environ.get("CF_ACCESS_AUD", "").strip()
    return raw or None


def _get_jwks_client(team_domain: str) -> PyJWKClient:
    now = time.monotonic()
    cached = _JWKS_CACHE.get(team_domain)
    if cached is not None and now - cached[1] < _JWKS_TTL_SECONDS:
        return cached[0]
    client = PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs")
    _JWKS_CACHE[team_domain] = (client, now)
    return client


def _staff_allowlist() -> frozenset[str]:
    raw = os.environ.get("SAE_STAFF_USERNAMES", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


class CFAccessAuthMiddleware(BaseHTTPMiddleware):
    """If a valid CF Access JWT is present and the user is not already
    logged in, mint a session via ``/api/v1/auth/oauth-handoff``.

    Failure modes are silent (logged, but request continues to whatever
    auth path is next): we never block a request from this middleware,
    because saebooks's existing login flow is a valid fallback. The only
    job here is "if CF Access verified them, skip the second login."
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _enabled():
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        if request.session.get("api_token"):
            return await call_next(request)

        token = request.headers.get("cf-access-jwt-assertion") or ""
        if not token:
            return await call_next(request)

        team = _team_domain()
        aud = _aud()
        if not team or not aud:
            _log.warning("CF Access trust enabled but CF_ACCESS_TEAM_DOMAIN/AUD unset")
            return await call_next(request)

        try:
            signing_key = _get_jwks_client(team).get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=aud,
                issuer=f"https://{team}",
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            _log.warning("CF Access JWT rejected (%s) on %s", exc, path)
            return await call_next(request)
        except Exception as exc:
            _log.warning("CF Access JWT verification error (%s) on %s", exc, path)
            return await call_next(request)

        email = (claims.get("email") or "").strip().lower()
        sub = (claims.get("sub") or "").strip()
        if not email or not sub:
            _log.warning("CF Access JWT missing email/sub on %s", path)
            return await call_next(request)

        secret = os.environ.get("SAEBOOKS_OAUTH_HANDOFF_SECRET", "")
        if not secret:
            _log.warning("SAEBOOKS_OAUTH_HANDOFF_SECRET unset; cannot mint session for %s", email)
            return await call_next(request)

        display_name = (claims.get("name") or claims.get("nickname") or email)

        try:
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=5.0,
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/oauth-handoff",
                    headers={"X-OAuth-Handoff-Secret": secret},
                    json={
                        "provider": "cloudflare-access",
                        "provider_user_id": sub,
                        "email": email,
                        "display_name": display_name,
                    },
                )
                if not resp.is_success:
                    _log.warning("oauth-handoff %s for %s: %s", resp.status_code, email, resp.text[:200])
                    return await call_next(request)
                token_resp = resp.json()
                api_token = token_resp["access_token"]
                me_resp = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {api_token}"},
                )
        except httpx.RequestError as exc:
            _log.warning("oauth-handoff transport error for %s: %s", email, exc)
            return await call_next(request)

        request.session.pop("csrf_token", None)
        request.session["api_token"] = api_token

        if me_resp.is_success:
            profile = me_resp.json()
            request.session["username"] = (
                profile.get("name") or profile.get("username") or profile.get("email") or ""
            )
            request.session["user_role"] = profile.get("role", "")
            allow = _staff_allowlist()
            uname = (profile.get("username") or "").lower()
            uemail = (profile.get("email") or "").lower()
            request.session["is_sae_staff"] = bool(allow and (uname in allow or uemail in allow))
        else:
            request.session["is_sae_staff"] = False
            request.session["user_role"] = ""

        _log.info("CF Access session minted for %s on %s", email, path)
        return await call_next(request)
