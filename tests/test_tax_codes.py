"""Tests for the tax codes list + detail views — Lane D cycle 9.

Three tests:
1. test_tax_codes_requires_auth    — 303 → /login without session
2. test_tax_codes_list_renders     — full-page render contains tax code
3. test_tax_codes_detail_renders   — detail page shows tax code name
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

_TAX_CODE_ID = "eeeeeeee-eeee-eeee-eeee-444444444444"

_MOCK_TAX_CODE = {
    "id": _TAX_CODE_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "GST",
    "name": "GST on Sales",
    "rate": "10.0",
    "tax_system": "GST",
    "reporting_type": "taxable",
    "description": "Standard 10% GST",
    "version": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}

_MOCK_TAX_CODES_RESPONSE = {
    "items": [_MOCK_TAX_CODE],
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
async def test_tax_codes_requires_auth() -> None:
    """GET /tax-codes without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/tax-codes")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_tax_codes_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /tax-codes renders the code in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/tax-codes")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "GST on Sales" in resp.text
    assert "10.0%" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_tax_codes_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /tax-codes/{id} renders the tax code name on the detail page."""
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes/{_TAX_CODE_ID}").mock(
        return_value=Response(200, json=_MOCK_TAX_CODE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/tax-codes/{_TAX_CODE_ID}")

    assert resp.status_code == 200
    assert "GST on Sales" in resp.text
    assert "10.0%" in resp.text
