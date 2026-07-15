"""Tests for the bank accounts list + detail views — Lane D cycle 27.

Three tests:
1. test_bank_accounts_list_renders      — full-page GET 200 with account code in body
2. test_bank_accounts_list_htmx_partial — HX-Request returns fragment (no <html>)
3. test_bank_accounts_detail_renders    — detail page shows code, name, BSB
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

_ACCOUNT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-000000000001"

_MOCK_ACCOUNT = {
    "id": _ACCOUNT_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "BNKANZ001",
    "name": "ANZ Business Cheque",
    "bsb": "012-345",
    "bank_account_number": "123456789",
    "bank_account_title": "SAE Engineering Pty Ltd",
    "apca_user_id": None,
    "bank_abbreviation": "ANZ",
    "version": 1,
    "created_at": "2024-06-01T09:00:00Z",
    "archived_at": None,
    # The list route requests include_balance=true&include_statement_balance=true
    # and the table template renders these unconditionally when not None.
    "balance": 5000.0,
    "statement_balance": 5000.0,
}

_MOCK_ACCOUNTS_RESPONSE = {
    "items": [_MOCK_ACCOUNT],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_accounts_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /bank-accounts renders account code and name in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-accounts")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "BNKANZ001" in resp.text
    assert "ANZ Business Cheque" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_accounts_list_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /bank-accounts with HX-Request header returns fragment, not full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/bank-accounts",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "BNKANZ001" in resp.text
    assert "bank-accounts-table" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_accounts_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /bank-accounts/{id} renders account code, name, BSB."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bank-accounts/{_ACCOUNT_ID}")

    assert resp.status_code == 200
    assert "BNKANZ001" in resp.text
    assert "ANZ Business Cheque" in resp.text
    assert "012-345" in resp.text
    assert "123456789" in resp.text
