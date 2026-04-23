"""Tests for the items list + detail views — Lane D cycle 9.

Three tests:
1. test_items_requires_auth    — 303 → /login without session
2. test_items_list_renders     — full-page render contains item SKU
3. test_items_detail_renders   — detail page shows item name
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

_ITEM_ID = "bbbbbbbb-bbbb-bbbb-bbbb-222222222222"
_ACCOUNT_ID = "cccccccc-cccc-cccc-cccc-333333333333"

_MOCK_ITEM = {
    "id": _ITEM_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "sku": "WIDGET-001",
    "name": "Blue Widget",
    "item_type": "inventory",
    "description": "A fine blue widget",
    "cost_method": "WAC",
    "default_sale_price": "49.99",
    "on_hand_qty": "150",
    "wac_cost": "22.5000",
    "inventory_account_id": _ACCOUNT_ID,
    "cogs_account_id": _ACCOUNT_ID,
    "income_account_id": _ACCOUNT_ID,
    "version": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}

_MOCK_ITEMS_RESPONSE = {
    "items": [_MOCK_ITEM],
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
async def test_items_requires_auth() -> None:
    """GET /items without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/items")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_items_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /items renders the SKU in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/items").mock(
        return_value=Response(200, json=_MOCK_ITEMS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/items")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "WIDGET-001" in resp.text
    assert "Blue Widget" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_items_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /items/{id} renders the item name on the detail page."""
    respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=_MOCK_ITEM)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/items/{_ITEM_ID}")

    assert resp.status_code == 200
    assert "Blue Widget" in resp.text
    assert "WIDGET-001" in resp.text
