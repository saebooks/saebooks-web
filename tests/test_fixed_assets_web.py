"""Tests for the fixed assets list + detail views — Lane D cycle 26.

Four tests:
1. test_fixed_assets_list_renders       — full-page GET 200 with asset code in body
2. test_fixed_assets_list_htmx_partial  — HX-Request returns fragment (no <html>)
3. test_fixed_assets_detail_renders     — detail page shows asset code + name
4. test_fixed_assets_detail_404         — upstream 404 → HTTP 404 response
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

_ASSET_ID = "aaaaaaaa-aaaa-aaaa-aaaa-000000000001"

_MOCK_DEPRECIATION_MODEL = {
    "id": "SL-10Y",
    "method": "STRAIGHT_LINE",
    "method_number": 1,
    "method_period": 120,
}

_MOCK_ASSET = {
    "id": _ASSET_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "AST-000001",
    "name": "Office Server Rack",
    "description": "Main server rack in comms room",
    "status": "ACTIVE",
    "depreciation_model_id": "SL-10Y",
    "depreciation_model": _MOCK_DEPRECIATION_MODEL,
    "tax_model_id": None,
    "cost_account_id": "dddddddd-dddd-dddd-dddd-000000000001",
    "accum_dep_account_id": "dddddddd-dddd-dddd-dddd-000000000002",
    "dep_expense_account_id": "dddddddd-dddd-dddd-dddd-000000000003",
    "purchase_date": "2024-01-15",
    "in_service_date": "2024-01-20",
    "cost": "12500.00",
    "residual_value": "500.00",
    "last_depreciation_posted_through": "2026-03-31",
    "disposal_date": None,
    "disposal_proceeds": None,
    "disposal_journal_id": None,
    "serial_number": "SN-RACK-001",
    "manufacturer": "APC",
    "model_number": "AR3100",
    "location": "Comms Room A",
    "custody_person": "Richard Sauer",
    "warranty_end": "2027-01-15",
    "extra": None,
    "version": 3,
    "created_at": "2024-01-15T09:00:00Z",
    "updated_at": "2026-04-01T12:00:00Z",
    "archived_at": None,
}

_MOCK_ASSETS_RESPONSE = {
    "items": [_MOCK_ASSET],
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
async def test_fixed_assets_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /fixed-assets renders asset code in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets").mock(
        return_value=Response(200, json=_MOCK_ASSETS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/fixed-assets")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "AST-000001" in resp.text
    assert "Office Server Rack" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_fixed_assets_list_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets with HX-Request header returns fragment, not full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets").mock(
        return_value=Response(200, json=_MOCK_ASSETS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/fixed-assets",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "AST-000001" in resp.text
    assert "fixed-assets-table" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_fixed_assets_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets/{id} renders asset code, name, and depreciation details."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/fixed-assets/{_ASSET_ID}")

    assert resp.status_code == 200
    assert "AST-000001" in resp.text
    assert "Office Server Rack" in resp.text
    # Depreciation model details
    assert "SL-10Y" in resp.text
    # Cost and residual value
    assert "12500" in resp.text
    assert "500" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_fixed_assets_detail_404(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(404, json={"detail": "Fixed asset not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/fixed-assets/{_ASSET_ID}")

    assert resp.status_code == 404
    assert "not found" in resp.text.lower()
