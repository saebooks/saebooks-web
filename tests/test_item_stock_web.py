"""Tests for the live stock panel on the item detail page.

The engine's GET /api/v1/items/{id}/stock is the canonical stock read for
inventory items (adds engine-computed inventory_value = on_hand_qty × WAC).
There is NO stock-adjustment endpoint on the engine — the panel is view-only
by design; stock moves via posted bills/invoices.

Four tests:
1. test_stock_panel_renders_for_inventory_item — panel shows qty, WAC, value
2. test_stock_not_fetched_for_service_item     — no stock call for service items
3. test_stock_failure_degrades_gracefully      — stock 500 -> page still 200, no panel
4. test_stock_not_fetched_for_archived_item    — no stock call for archived items
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

_ITEM_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ACCOUNT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_MOCK_INVENTORY_ITEM = {
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

_MOCK_SERVICE_ITEM = {
    **_MOCK_INVENTORY_ITEM,
    "sku": "CONSULT-001",
    "name": "Consulting Hour",
    "item_type": "service",
    "on_hand_qty": "0",
    "wac_cost": "0.0000",
}

_MOCK_STOCK = {
    "item_id": _ITEM_ID,
    "sku": "WIDGET-001",
    "item_type": "inventory",
    "on_hand_qty": "150",
    "wac_cost": "22.5000",
    "inventory_value": "3375.0000",
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


async def _get_detail(respx_mock: respx.MockRouter, item: dict, stock_response: Response | None):
    respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=item)
    )
    if stock_response is not None:
        respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}/stock").mock(
            return_value=stock_response
        )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        return await client.get(f"/items/{_ITEM_ID}")


@pytest.mark.anyio
@respx.mock
async def test_stock_panel_renders_for_inventory_item(respx_mock: respx.MockRouter) -> None:
    resp = await _get_detail(respx_mock, _MOCK_INVENTORY_ITEM, Response(200, json=_MOCK_STOCK))

    assert resp.status_code == 200
    assert "Inventory value" in resp.text
    # Engine-computed value rendered with 2dp money formatting.
    assert "3,375.00" in resp.text or "3375.00" in resp.text
    assert "150" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_stock_not_fetched_for_service_item(respx_mock: respx.MockRouter) -> None:
    """Service items have no stock tracking — the engine would 404 the call,
    so the page never makes it."""
    resp = await _get_detail(respx_mock, _MOCK_SERVICE_ITEM, None)

    assert resp.status_code == 200
    assert "Consulting Hour" in resp.text
    assert "Inventory value" not in resp.text
    assert not any("/stock" in str(c.request.url) for c in respx_mock.calls)


@pytest.mark.anyio
@respx.mock
async def test_stock_failure_degrades_gracefully(respx_mock: respx.MockRouter) -> None:
    """A failed stock read hides the panel — never errors the detail page."""
    resp = await _get_detail(respx_mock, _MOCK_INVENTORY_ITEM, Response(500, json={"detail": "boom"}))

    assert resp.status_code == 200
    assert "Blue Widget" in resp.text
    assert "Inventory value" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_stock_not_fetched_for_archived_item(respx_mock: respx.MockRouter) -> None:
    archived = {**_MOCK_INVENTORY_ITEM, "archived_at": "2026-05-01T00:00:00Z"}
    resp = await _get_detail(respx_mock, archived, None)

    assert resp.status_code == 200
    assert not any("/stock" in str(c.request.url) for c in respx_mock.calls)
