"""Tests for the item edit form — Lane D cycle 22.

Five tests:
1. test_item_edit_form_renders          — GET /items/{id}/edit has version hidden input + pre-filled values
2. test_item_edit_archived_blocked      — GET /items/{id}/edit on archived item -> 422 + edit_blocked
3. test_item_edit_success_redirects     — POST happy path; API 200 -> 303 to detail
4. test_item_edit_conflict_shows_banner — API 409 -> re-render with conflict banner + server version
5. test_item_edit_validation_error      — API 422 -> re-render with field errors, input preserved
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

_ITEM_ID = "bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb"
_ACCOUNT_ID_1 = "cccccccc-1111-1111-1111-cccccccccccc"
_ACCOUNT_ID_2 = "cccccccc-2222-2222-2222-cccccccccccc"
_ACCOUNT_ID_3 = "cccccccc-3333-3333-3333-cccccccccccc"

_MOCK_ITEM = {
    "id": _ITEM_ID,
    "sku": "WIDGET-002",
    "name": "Widget, 20mm",
    "item_type": "inventory",
    "description": "A medium-sized widget",
    "cost_method": "WAC",
    "default_sale_price": "20.00",
    "inventory_account_id": _ACCOUNT_ID_1,
    "cogs_account_id": _ACCOUNT_ID_2,
    "income_account_id": _ACCOUNT_ID_3,
    "on_hand_qty": "5",
    "wac_cost": "8.50",
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 3,
    "created_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_ITEM_ARCHIVED = {
    **_MOCK_ITEM,
    "archived_at": "2026-04-24T10:00:00Z",
    "version": 4,
}

# Server version returned after a 409 conflict.
_MOCK_ITEM_V4 = {
    **_MOCK_ITEM,
    "version": 4,
    "name": "Widget, 20mm (updated by someone else)",
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
# 1. GET /items/{id}/edit — renders form with version and pre-filled values
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /items/{id}/edit renders the form with a hidden version input and pre-filled fields."""
    respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=_MOCK_ITEM)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/items/{_ITEM_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with correct value.
    assert 'name="version"' in resp.text
    assert 'value="3"' in resp.text
    # Fields are pre-filled.
    assert "WIDGET-002" in resp.text
    assert "Widget, 20mm" in resp.text
    assert 'name="sku"' in resp.text
    assert 'name="name"' in resp.text


# ---------------------------------------------------------------------------
# 2. GET /items/{id}/edit on archived item -> 422 + edit_blocked template
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_edit_archived_blocked(respx_mock: respx.MockRouter) -> None:
    """GET /items/{id}/edit for an archived item returns 422 and the edit_blocked page."""
    respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=_MOCK_ITEM_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/items/{_ITEM_ID}/edit")

    assert resp.status_code == 422
    assert "Archived items cannot be edited" in resp.text
    assert "Restore it first" in resp.text


# ---------------------------------------------------------------------------
# 3. POST /items/{id}/edit — happy path -> 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /items/{id}/edit with correct version; API 200 -> 303 to detail."""
    respx_mock.patch(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=_MOCK_ITEM)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/items/{_ITEM_ID}/edit",
            data={
                "sku": "WIDGET-002",
                "name": "Widget, 20mm Updated",
                "version": "3",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/items/{_ITEM_ID}"


# ---------------------------------------------------------------------------
# 4. POST /items/{id}/edit — 409 conflict -> banner + latest server version
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> re-render form with conflict banner."""
    respx_mock.patch(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # Route re-fetches the item after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=_MOCK_ITEM_V4)
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
            f"/items/{_ITEM_ID}/edit",
            data={
                "sku": "WIDGET-002",
                "name": "My Edited Name",
                "version": "3",  # stale
            },
        )

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else has updated this item" in resp.text
    # Hidden version updated to server's latest (4).
    assert 'value="4"' in resp.text
    # User's submitted name preserved.
    assert "My Edited Name" in resp.text


# ---------------------------------------------------------------------------
# 5. POST /items/{id}/edit — 422 validation error -> re-render with errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /items/{id}/edit where API returns 422 re-renders with field errors."""
    _422_body = {
        "detail": [
            {
                "type": "string_too_short",
                "loc": ["body", "sku"],
                "msg": "String should have at least 1 character",
                "input": "",
            }
        ]
    }
    respx_mock.patch(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
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
            f"/items/{_ITEM_ID}/edit",
            data={
                "sku": "",
                "name": "Widget With No SKU",
                "version": "3",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered (not blank).
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "Widget With No SKU" in resp.text
    # Field error visible.
    assert "String should have at least 1 character" in resp.text
