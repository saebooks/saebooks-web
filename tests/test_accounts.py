"""Tests for the accounts list + detail views — Lane D cycle 9.

Three tests:
1. test_accounts_requires_auth    — 303 → /login without session
2. test_accounts_list_renders     — full-page render contains account code
3. test_accounts_detail_renders   — detail page shows account name
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-111111111111"

_MOCK_ACCOUNT = {
    "id": _ACCOUNT_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "1-1000",
    "name": "Business Bank Account",
    "account_type": "ASSET",
    "parent_id": None,
    "tax_code_default": None,
    "is_header": False,
    "reconcile": True,
    "system_managed": False,
    "bsb": "012-345",
    "bank_account_number": "123456789",
    "bank_account_title": "SAE Engineering",
    "apca_user_id": None,
    "bank_abbreviation": None,
    "version": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}

_MOCK_ACCOUNTS_RESPONSE = {
    "items": [_MOCK_ACCOUNT],
    "total": 1,
    "limit": 200,
    "offset": 0,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_accounts_requires_auth() -> None:
    """GET /accounts without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_accounts_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /accounts renders the account code in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "1-1000" in resp.text
    assert "Business Bank Account" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_accounts_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /accounts/{id} renders the account name on the detail page."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/accounts/{_ACCOUNT_ID}")

    assert resp.status_code == 200
    assert "Business Bank Account" in resp.text
    assert "1-1000" in resp.text
