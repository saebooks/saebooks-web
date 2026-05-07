"""Tests for the item create form — Lane D cycle 22.

Five tests:
1. test_item_new_form_renders          — GET /items/new returns form with all fields
2. test_item_create_success_redirects  — POST happy path -> 303 to /items/{id}
3. test_item_create_validation_error   — POST 422 -> re-render form with errors
4. test_item_create_duplicate_sku      — POST 422 string detail -> __all__ banner
5. test_item_create_sends_idempotency_key — POST includes X-Idempotency-Key header
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

_ITEM_ID = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
_ACCOUNT_ID_1 = "cccccccc-1111-1111-1111-cccccccccccc"
_ACCOUNT_ID_2 = "cccccccc-2222-2222-2222-cccccccccccc"
_ACCOUNT_ID_3 = "cccccccc-3333-3333-3333-cccccccccccc"

_MOCK_ITEM = {
    "id": _ITEM_ID,
    "sku": "WIDGET-001",
    "name": "Widget, 10mm",
    "item_type": "inventory",
    "description": None,
    "cost_method": "WAC",
    "default_sale_price": "10.00",
    "inventory_account_id": _ACCOUNT_ID_1,
    "cogs_account_id": _ACCOUNT_ID_2,
    "income_account_id": _ACCOUNT_ID_3,
    "on_hand_qty": "0",
    "wac_cost": "0",
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_ACCOUNTS = {
    "items": [
        {"id": _ACCOUNT_ID_1, "code": "1100", "name": "Inventory Asset"},
        {"id": _ACCOUNT_ID_2, "code": "5000", "name": "Cost of Goods Sold"},
        {"id": _ACCOUNT_ID_3, "code": "4000", "name": "Sales Revenue"},
    ],
    "total": 3,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /items/new — form renders with expected fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /items/new returns the form with all expected fields."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/items/new")

    assert resp.status_code == 200
    assert 'name="sku"' in resp.text
    assert 'name="name"' in resp.text
    assert 'name="item_type"' in resp.text
    assert 'name="default_sale_price"' in resp.text
    assert 'name="income_account_id"' in resp.text
    assert 'name="cogs_account_id"' in resp.text
    assert 'name="inventory_account_id"' in resp.text
    assert 'name="description"' in resp.text
    assert 'name="idempotency_key"' in resp.text


# ---------------------------------------------------------------------------
# 2. POST /items/new happy path -> 303 redirect to /items/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /items/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/items").mock(
        return_value=Response(201, json=_MOCK_ITEM)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/items/new",
            data={
                "sku": "WIDGET-001",
                "name": "Widget, 10mm",
                "item_type": "inventory",
                "inventory_account_id": _ACCOUNT_ID_1,
                "cogs_account_id": _ACCOUNT_ID_2,
                "income_account_id": _ACCOUNT_ID_3,
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/items/{_ITEM_ID}"


# ---------------------------------------------------------------------------
# 3. POST /items/new — 422 per-field validation error -> re-render
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /items/new where API returns 422 re-renders the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "sku"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/items").mock(
        return_value=Response(422, json=_422_body)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/items/new",
            data={
                "name": "Widget No SKU",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered, not a blank page.
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "Widget No SKU" in resp.text
    # Error text visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /items/new — 422 with string detail (duplicate SKU) -> __all__ banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_create_duplicate_sku(respx_mock: respx.MockRouter) -> None:
    """POST /items/new where API returns a plain string 422 detail -> __all__ banner."""
    respx_mock.post(f"{_API_BASE}/api/v1/items").mock(
        return_value=Response(422, json={"detail": "SKU already exists for this tenant."})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/items/new",
            data={
                "sku": "WIDGET-001",
                "name": "Widget Duplicate",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        )

    assert resp.status_code == 422
    # Non-field error banner should show the API message.
    assert "SKU already exists" in resp.text
    # Submitted sku preserved.
    assert "WIDGET-001" in resp.text


# ---------------------------------------------------------------------------
# 5. POST /items/new — X-Idempotency-Key header forwarded to API
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_create_sends_idempotency_key(respx_mock: respx.MockRouter) -> None:
    """POST /items/new passes the idempotency_key field as X-Idempotency-Key header."""
    _idem_key = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("x-idempotency-key", ""))
        return Response(201, json=_MOCK_ITEM)

    respx_mock.post(f"{_API_BASE}/api/v1/items").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/items/new",
            data={
                "sku": "IDEM-001",
                "name": "Idempotent Widget",
                "item_type": "inventory",
                "inventory_account_id": _ACCOUNT_ID_1,
                "cogs_account_id": _ACCOUNT_ID_2,
                "income_account_id": _ACCOUNT_ID_3,
                "idempotency_key": _idem_key,
            },
        )

    assert len(captured) == 1, "Expected exactly one upstream POST call"
    assert captured[0] == _idem_key, f"Expected idempotency key {_idem_key!r}, got {captured[0]!r}"
