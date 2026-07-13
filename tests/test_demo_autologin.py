"""Tests for the ephemeral demo autologin middleware: binding-key reuse,
home-button fix, and Turnstile gate.

All upstream HTTP (saebooks-api + Cloudflare siteverify) is mocked via
``respx``.  Tests directly exercise ``DemoAutoLoginMiddleware`` via a
minimal stub ASGI app to avoid the noise of the full route stack.

Test matrix
-----------
1. Fresh visit GET / (no cookie, land=/dashboard) → provisions, 303 → /dashboard.
2. Returning visit GET / with valid binding key → REUSE (no provision), 303 → /dashboard.
3. Returning visit GET / with valid key, land=/ → REUSE, pass-through (no provision).
4. Stale binding key (reaped tenant, auth/me 401) → re-provision, 303.
5. Provision failure (capacity / 429) → pass-through, no 500.
6. Turnstile OFF — fresh visit provisions directly (same as test 1).
7. Turnstile ON — fresh visit GET / → returns landing page (no provision call).
8. Turnstile ON — POST to /demo/turnstile-provision with valid token → provisions, 303.
9. Turnstile ON — POST with invalid token → gate page shown, no provision.
10. Turnstile ON — returning visitor with valid key on GET / → REUSE, no gate page.
11. Skip prefixes bypass middleware entirely.
"""
from __future__ import annotations

import json
import os
from base64 import b64encode
from unittest.mock import patch

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from saebooks_web.config import settings
from saebooks_web.security.demo_autologin import DemoAutoLoginMiddleware

# ---------------------------------------------------------------------------
# Stub ASGI app — minimal app with only SessionMiddleware and
# DemoAutoLoginMiddleware so tests are isolated from the full route stack.
# ---------------------------------------------------------------------------


def _make_stub_app(env: dict):  # noqa: ANN001  — returns an ASGI app
    """Return a fresh ASGI app with the demo middleware.

    Real app middleware stack order (outer → inner, i.e. request processing order):
      SessionMiddleware → ... → DemoAutoLoginMiddleware → ... → routes

    SessionMiddleware MUST run first (outermost) so it populates
    ``request.session`` before DemoAutoLoginMiddleware reads it.
    In the real app this happens because ``add_middleware`` inserts at index 0
    (outermost) and SessionMiddleware is added last in source.

    To replicate: SessionMiddleware wraps DemoAutoLoginMiddleware wraps the
    catch-all. This means the function-call stack is:
      SessionMiddleware.__call__
        → DemoAutoLoginMiddleware.__call__
          → catch-all
    """

    async def _catch_all(scope, receive, send) -> None:
        response = PlainTextResponse("OK")
        await response(scope, receive, send)

    # Inner: DemoAutoLoginMiddleware wraps the catch-all route.
    demo_wrapped = DemoAutoLoginMiddleware(_catch_all)
    # Outer: SessionMiddleware runs first (populates scope["session"]).
    return SessionMiddleware(
        demo_wrapped,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age,
    )


# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")
_PROVISION_URL = f"{_API_BASE}/internal/demo/provision"
_AUTH_ME_URL = f"{_API_BASE}/api/v1/auth/me"
_TURNSTILE_SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

_FAKE_TOKEN_LIVE = "demo-token-live-abc123"
_FAKE_TOKEN_STALE = "demo-token-stale-xyz789"
_FAKE_TOKEN_NEW = "demo-token-new-provisioned"
_FAKE_COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_FAKE_COMPANY_ID_NEW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_PROVISION_RESP = {
    "access_token": _FAKE_TOKEN_NEW,
    "company_id": _FAKE_COMPANY_ID_NEW,
    "demo_user_email": "demo@example.com",
}
_ME_RESP_NEW = {
    "email": "demo@example.com",
    "name": "Demo User",
    "role": "admin",
    "company_id": _FAKE_COMPANY_ID_NEW,
}
_ME_RESP_LIVE = {
    "email": "demo@example.com",
    "name": "Demo User",
    "role": "admin",
    "company_id": _FAKE_COMPANY_ID,
}

# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = TimestampSigner(settings.secret_key)
    payload = b64encode(json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _session_with_live_token() -> str:
    return _make_session_cookie({"api_token": _FAKE_TOKEN_LIVE})


def _session_with_stale_token() -> str:
    return _make_session_cookie({"api_token": _FAKE_TOKEN_STALE})


# ---------------------------------------------------------------------------
# Environment config helpers
# ---------------------------------------------------------------------------

_EPHEMERAL_ENV = {
    "SAEBOOKS_DEMO_EPHEMERAL": "1",
    "DEMO_INTERNAL_SECRET": "test-secret",
    "SAEBOOKS_DEMO_LAND_PATH": "/dashboard",
    "SAEBOOKS_DEMO_TURNSTILE_ENABLED": "0",
}

_TURNSTILE_ENV = {
    **_EPHEMERAL_ENV,
    "SAEBOOKS_DEMO_TURNSTILE_ENABLED": "1",
    "TURNSTILE_SITE_KEY": "test-site-key-0x1234",
    "TURNSTILE_SECRET_KEY": "test-secret-key-0xabcd",
}


def _client(env: dict, cookie: str | None = None) -> AsyncClient:
    """Build an httpx AsyncClient hitting the stub app with the given env patched."""
    stub = _make_stub_app(env)
    cookies = {}
    if cookie:
        cookies[settings.session_cookie_name] = cookie
    return AsyncClient(
        transport=ASGITransport(stub),
        base_url="http://test",
        cookies=cookies,
    )


# ---------------------------------------------------------------------------
# Test 1: fresh visit GET / (no cookie) → provision, redirect to land path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fresh_visit_provisions(respx_mock: respx.MockRouter) -> None:
    """No binding key → provision a fresh tenant, write session, redirect to /dashboard."""
    respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json=_ME_RESP_NEW)
    )

    with patch.dict(os.environ, _EPHEMERAL_ENV, clear=False):
        async with _client(_EPHEMERAL_ENV) as client:
            resp = await client.get("/", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    # Session cookie must be present.
    assert settings.session_cookie_name in resp.cookies
    # Provision endpoint was called exactly once.
    provision_calls = [c for c in respx_mock.calls if "/internal/demo/provision" in str(c.request.url)]
    assert len(provision_calls) == 1


# ---------------------------------------------------------------------------
# Test 2: returning visit GET / with valid binding key → REUSE, no provision
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_returning_visit_reuses_company(respx_mock: respx.MockRouter) -> None:
    """Valid binding key on GET / → reuse the company, no provision call."""
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json=_ME_RESP_LIVE)
    )
    provision_route = respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    with patch.dict(os.environ, _EPHEMERAL_ENV, clear=False):
        async with _client(_EPHEMERAL_ENV, cookie=_session_with_live_token()) as client:
            resp = await client.get("/", follow_redirects=False)

    # Should redirect to land path (company reused, path was /).
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    # Provision must NOT have been called.
    assert provision_route.call_count == 0


# ---------------------------------------------------------------------------
# Test 3: valid key on /, land path = / → pass-through (no redirect, no provision)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_returning_visit_land_root_passthrough(respx_mock: respx.MockRouter) -> None:
    """Valid key, land_path=/ → no redirect, pass-through to route, no provision."""
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json=_ME_RESP_LIVE)
    )
    provision_route = respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    env = {**_EPHEMERAL_ENV, "SAEBOOKS_DEMO_LAND_PATH": "/"}
    with patch.dict(os.environ, env, clear=False):
        async with _client(env, cookie=_session_with_live_token()) as client:
            resp = await client.get("/", follow_redirects=False)

    assert resp.status_code == 200
    assert provision_route.call_count == 0


# ---------------------------------------------------------------------------
# Test 4: stale binding key (reaped tenant) → re-provision
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stale_key_reprovisions(respx_mock: respx.MockRouter) -> None:
    """Stale key (auth/me 401) → re-provision a fresh tenant."""

    def me_side_effect(request):
        auth = request.headers.get("authorization", "")
        if _FAKE_TOKEN_STALE in auth:
            return Response(401, json={"detail": "token expired"})
        return Response(200, json=_ME_RESP_NEW)

    respx_mock.get(_AUTH_ME_URL).mock(side_effect=me_side_effect)
    respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    with patch.dict(os.environ, _EPHEMERAL_ENV, clear=False):
        async with _client(_EPHEMERAL_ENV, cookie=_session_with_stale_token()) as client:
            resp = await client.get("/", follow_redirects=False)

    assert resp.status_code == 303
    provision_calls = [c for c in respx_mock.calls if "/internal/demo/provision" in str(c.request.url)]
    assert len(provision_calls) == 1


# ---------------------------------------------------------------------------
# Test 5: provision failure → pass-through (no 500)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_provision_failure_passthrough(respx_mock: respx.MockRouter) -> None:
    """Capacity exceeded (429) → let the request through; no 500."""
    respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(429, json={"detail": "capacity"})
    )

    with patch.dict(os.environ, _EPHEMERAL_ENV, clear=False):
        async with _client(_EPHEMERAL_ENV) as client:
            resp = await client.get("/", follow_redirects=False)

    assert resp.status_code != 500
    # Pass-through gives 200 from the stub route.
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test 6: Turnstile OFF — fresh visit provisions directly
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_turnstile_off_fresh_provisions_directly(respx_mock: respx.MockRouter) -> None:
    """Turnstile disabled — fresh visit provisions directly, no HTML gate page."""
    respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json=_ME_RESP_NEW)
    )

    with patch.dict(os.environ, _EPHEMERAL_ENV, clear=False):  # TURNSTILE_ENABLED=0
        async with _client(_EPHEMERAL_ENV) as client:
            resp = await client.get("/", follow_redirects=False)

    # Should provision and redirect, NOT return a 200 gate page.
    assert resp.status_code == 303
    provision_calls = [c for c in respx_mock.calls if "/internal/demo/provision" in str(c.request.url)]
    assert len(provision_calls) == 1


# ---------------------------------------------------------------------------
# Test 7: Turnstile ON — fresh visit GET / → gate page returned (no provision)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_turnstile_on_fresh_visit_shows_gate(respx_mock: respx.MockRouter) -> None:
    """Turnstile enabled, no key → GET / returns the Turnstile challenge page."""
    provision_route = respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    with patch.dict(os.environ, _TURNSTILE_ENV, clear=False):
        async with _client(_TURNSTILE_ENV) as client:
            resp = await client.get("/", follow_redirects=False)

    assert resp.status_code == 200
    assert "cf-turnstile" in resp.text
    assert "test-site-key-0x1234" in resp.text
    # Provision must NOT be called before challenge completion.
    assert provision_route.call_count == 0


# ---------------------------------------------------------------------------
# Test 8: Turnstile ON — valid token POST → provisions and redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_turnstile_on_valid_token_provisions(respx_mock: respx.MockRouter) -> None:
    """Turnstile enabled; POST /demo/turnstile-provision with valid token → provision."""
    respx_mock.post(_TURNSTILE_SITEVERIFY).mock(
        return_value=Response(200, json={"success": True})
    )
    respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json=_ME_RESP_NEW)
    )

    with patch.dict(os.environ, _TURNSTILE_ENV, clear=False):
        async with _client(_TURNSTILE_ENV) as client:
            resp = await client.post(
                "/demo/turnstile-provision",
                content="cf-turnstile-response=fake-turnstile-token-abc",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert settings.session_cookie_name in resp.cookies
    provision_calls = [c for c in respx_mock.calls if "/internal/demo/provision" in str(c.request.url)]
    assert len(provision_calls) == 1


# ---------------------------------------------------------------------------
# Test 9: Turnstile ON — invalid token → gate page, no provision
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_turnstile_on_invalid_token_no_provision(respx_mock: respx.MockRouter) -> None:
    """Turnstile enabled; bad token → gate page shown, provision NOT called."""
    respx_mock.post(_TURNSTILE_SITEVERIFY).mock(
        return_value=Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )
    )
    provision_route = respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    with patch.dict(os.environ, _TURNSTILE_ENV, clear=False):
        async with _client(_TURNSTILE_ENV) as client:
            resp = await client.post(
                "/demo/turnstile-provision",
                content="cf-turnstile-response=bad-token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )

    assert resp.status_code == 200
    assert "cf-turnstile" in resp.text
    assert "Verification failed" in resp.text
    assert provision_route.call_count == 0


# ---------------------------------------------------------------------------
# Test 10: Turnstile ON — returning visitor with valid key → REUSE, no gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_turnstile_on_returning_visitor_reuses(respx_mock: respx.MockRouter) -> None:
    """Turnstile enabled, valid binding key → reuse, NO gate page, NO provision."""
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json=_ME_RESP_LIVE)
    )
    provision_route = respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    with patch.dict(os.environ, _TURNSTILE_ENV, clear=False):
        async with _client(_TURNSTILE_ENV, cookie=_session_with_live_token()) as client:
            resp = await client.get("/", follow_redirects=False)

    # Gate page must NOT be shown.
    assert "cf-turnstile" not in resp.text
    # Provision must NOT be called.
    assert provision_route.call_count == 0
    # Should be 303 (valid key, path=/, land=/dashboard).
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Test 11: skip prefixes bypass middleware (uses full app for /healthz)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_skip_prefixes_bypass_middleware(respx_mock: respx.MockRouter) -> None:
    """/healthz bypasses the demo middleware — no provision call."""
    from saebooks_web.main import app as main_app  # noqa: PLC0415

    provision_route = respx_mock.post(_PROVISION_URL).mock(
        return_value=Response(201, json=_PROVISION_RESP)
    )

    with patch.dict(os.environ, _EPHEMERAL_ENV, clear=False):
        async with AsyncClient(
            transport=ASGITransport(main_app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert provision_route.call_count == 0
