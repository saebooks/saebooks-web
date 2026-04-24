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

_VALID_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.sig"
_TOKEN_RESPONSE = {
    "access_token": _VALID_TOKEN,
    "token_type": "bearer",
    "expires_in": 28800,
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
