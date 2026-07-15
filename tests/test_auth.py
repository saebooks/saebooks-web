"""Tests for the email+password login flow — D/50.

Six tests:
1. test_login_page_renders             — GET /login shows email + password fields
2. test_login_success_redirects        — POST valid creds → 303, session stores access_token
3. test_login_wrong_password_401       — API 401 → form re-rendered with error message
4. test_login_unknown_email_401        — API 401 (unknown email) → form re-rendered
5. test_login_api_500_generic_error    — API 500 → generic error message
6. test_login_network_error            — httpx.RequestError → generic error message
"""
from __future__ import annotations

import json as _json
from base64 import b64decode as _b64decode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")
_LOGIN_URL = f"{_API_BASE}/api/v1/auth/login"
_ME_URL = f"{_API_BASE}/api/v1/auth/me"

_VALID_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.sig"
_TOKEN_RESPONSE = {
    "access_token": _VALID_TOKEN,
    "token_type": "bearer",
    "expires_in": 28800,
}

# Default /auth/me payload for an ordinary tenant user.
_TENANT_USER_ME = {
    "id": "11111111-1111-1111-1111-111111111111",
    "username": "user_one",
    "email": "user@example.com",
    "name": "User One",
    "role": "bookkeeper",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
}


def _decode_session_cookie(cookie_value: str) -> dict:
    """Decode a Starlette signed session cookie back to the original dict."""
    signer = _TimestampSigner(settings.secret_key)
    payload = signer.unsign(cookie_value.encode(), max_age=None)
    return _json.loads(_b64decode(payload))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_page_renders() -> None:
    """GET /login returns the login form with email and password fields."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")
    assert resp.status_code == 200
    assert 'name="email"' in resp.text
    assert 'name="password"' in resp.text
    # No token-paste input
    assert 'name="api_token"' not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_login_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST valid email+password → 303 redirect; session stores access_token."""
    respx_mock.post(_LOGIN_URL).mock(return_value=Response(200, json=_TOKEN_RESPONSE))
    respx_mock.get(_ME_URL).mock(return_value=Response(200, json=_TENANT_USER_ME))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post("/login", data={"email": "user@example.com", "password": "secret"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # Session cookie must contain the JWT, not the password.
    cookie = resp.cookies.get(settings.session_cookie_name)
    assert cookie is not None
    session = _decode_session_cookie(cookie)
    assert session["api_token"] == _VALID_TOKEN


@pytest.mark.anyio
@respx.mock
async def test_login_wrong_password_401(respx_mock: respx.MockRouter) -> None:
    """API 401 (wrong password) → form re-rendered with 'Invalid email or password'."""
    respx_mock.post(_LOGIN_URL).mock(
        return_value=Response(401, json={"detail": "Invalid credentials"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/login", data={"email": "user@example.com", "password": "wrong"})

    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text
    # Must still show the login form.
    assert 'name="email"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_login_unknown_email_401(respx_mock: respx.MockRouter) -> None:
    """API 401 (unknown email) → same form error as wrong password."""
    respx_mock.post(_LOGIN_URL).mock(
        return_value=Response(401, json={"detail": "Invalid credentials"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/login", data={"email": "nobody@example.com", "password": "x"})

    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_login_api_500_generic_error(respx_mock: respx.MockRouter) -> None:
    """API 500 → form re-rendered with generic 'Login failed' message."""
    respx_mock.post(_LOGIN_URL).mock(return_value=Response(500, json={"detail": "oops"}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/login", data={"email": "user@example.com", "password": "pw"})

    assert resp.status_code == 502
    assert "Login failed" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_login_network_error(respx_mock: respx.MockRouter) -> None:
    """Network error → form re-rendered with generic 'Login failed' message."""
    import httpx as _httpx
    respx_mock.post(_LOGIN_URL).mock(side_effect=_httpx.ConnectError("refused"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/login", data={"email": "user@example.com", "password": "pw"})

    assert resp.status_code == 502
    assert "Login failed" in resp.text


# ---------------------------------------------------------------------------
# P0 regression tests — admin-gate fail-closed bug introduced by 5db97ad.
#
# Two compounding bugs were diagnosed by Taylor Riverside (Round 1):
#   (1) /auth/me was called OUTSIDE the `async with httpx.AsyncClient` block —
#       client closed, RuntimeError raised, swallowed by `except Exception: pass`,
#       so request.session["is_sae_staff"] was never written.
#   (2) /auth/me's response was missing the `username` field, so even after fix
#       (1), the allowlist check (`uname in allow`) compared "" to "richard".
#
# Fix: move the /auth/me call inside the with-block AND add `username` to the
# /auth/me response on the API side.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_login_sets_is_sae_staff_for_allowlisted_username(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter,
) -> None:
    """SAE_STAFF_USERNAMES=richard + /auth/me returns username=richard → is_sae_staff=True.

    Regression test: this is exactly the path that was failing in production
    after 5db97ad — richard was getting locked out of /admin/* because
    is_sae_staff was never being set.
    """
    monkeypatch.setenv("SAE_STAFF_USERNAMES", "richard")

    respx_mock.post(_LOGIN_URL).mock(return_value=Response(200, json=_TOKEN_RESPONSE))
    respx_mock.get(_ME_URL).mock(return_value=Response(200, json={
        "id": "22222222-2222-2222-2222-222222222222",
        "username": "richard",
        "email": "richard@sauer.com.au",
        "name": "Richard Sauer",
        "role": "admin",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
    }))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/login", data={"email": "richard@sauer.com.au", "password": "secret"},
        )

    assert resp.status_code == 303
    cookie = resp.cookies.get(settings.session_cookie_name)
    assert cookie is not None
    session = _decode_session_cookie(cookie)
    assert session["api_token"] == _VALID_TOKEN
    assert session["is_sae_staff"] is True, (
        f"is_sae_staff should be True for allowlisted user, got session={session}"
    )
    assert session["user_role"] == "admin"


@pytest.mark.anyio
@respx.mock
async def test_login_does_not_set_is_sae_staff_for_non_allowlisted_user(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter,
) -> None:
    """SAE_STAFF_USERNAMES=richard + /auth/me returns chen_apex → is_sae_staff=False.

    Tenant users (bookkeepers, tenant admins) must NOT get the staff flag.
    """
    monkeypatch.setenv("SAE_STAFF_USERNAMES", "richard")

    respx_mock.post(_LOGIN_URL).mock(return_value=Response(200, json=_TOKEN_RESPONSE))
    respx_mock.get(_ME_URL).mock(return_value=Response(200, json={
        "id": "33333333-3333-3333-3333-333333333333",
        "username": "chen_apex",
        "email": "chen_apex@critics.sauer.com.au",
        "name": "Chen Wei",
        "role": "bookkeeper",
        "tenant_id": "44444444-4444-4444-4444-444444444444",
    }))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/login", data={"email": "chen_apex@critics.sauer.com.au", "password": "secret"},
        )

    assert resp.status_code == 303
    cookie = resp.cookies.get(settings.session_cookie_name)
    assert cookie is not None
    session = _decode_session_cookie(cookie)
    assert session["is_sae_staff"] is False
    assert session["user_role"] == "bookkeeper"


@pytest.mark.anyio
@respx.mock
async def test_admin_audit_200_for_richard_after_login(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter,
) -> None:
    """End-to-end: richard logs in, then GET /admin/audit must be 200.

    This is the exact path Taylor's Probe C.2 failed on. With the fix in
    place, richard's session must have is_sae_staff=True after login, and
    /admin/audit must return 200 (not 403).
    """
    monkeypatch.setenv("SAE_STAFF_USERNAMES", "richard")

    respx_mock.post(_LOGIN_URL).mock(return_value=Response(200, json=_TOKEN_RESPONSE))
    respx_mock.get(_ME_URL).mock(return_value=Response(200, json={
        "id": "22222222-2222-2222-2222-222222222222",
        "username": "richard",
        "email": "richard@sauer.com.au",
        "name": "Richard Sauer",
        "role": "admin",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
    }))
    respx_mock.get(f"{_API_BASE}/api/v1/admin/audit-log").mock(
        return_value=Response(200, json={"items": [], "total": 0}),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        login_resp = await client.post(
            "/login", data={"email": "richard@sauer.com.au", "password": "secret"},
        )
        assert login_resp.status_code == 303

        # The AsyncClient persists cookies between calls.
        audit_resp = await client.get("/admin/audit")

    assert audit_resp.status_code == 200, (
        f"Expected 200 on /admin/audit for richard, got {audit_resp.status_code} — "
        "P0 admin-gate regression has returned"
    )


@pytest.mark.anyio
@respx.mock
async def test_admin_audit_403_for_chen_apex_after_login(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter,
) -> None:
    """End-to-end: chen_apex logs in, then GET /admin/audit must be 403.

    The bookkeeper personas must remain blocked from staff-only routes
    (Taylor's Probe A.2 — confirms the fix doesn't open the gate too wide).
    """
    monkeypatch.setenv("SAE_STAFF_USERNAMES", "richard")

    respx_mock.post(_LOGIN_URL).mock(return_value=Response(200, json=_TOKEN_RESPONSE))
    respx_mock.get(_ME_URL).mock(return_value=Response(200, json={
        "id": "33333333-3333-3333-3333-333333333333",
        "username": "chen_apex",
        "email": "chen_apex@critics.sauer.com.au",
        "name": "Chen Wei",
        "role": "bookkeeper",
        "tenant_id": "44444444-4444-4444-4444-444444444444",
    }))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        login_resp = await client.post(
            "/login", data={"email": "chen_apex@critics.sauer.com.au", "password": "secret"},
        )
        assert login_resp.status_code == 303

        audit_resp = await client.get("/admin/audit")

    assert audit_resp.status_code == 403, (
        f"Expected 403 on /admin/audit for chen_apex, got {audit_resp.status_code}"
    )
