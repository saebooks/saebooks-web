"""Tests for the item archive action — Lane D cycle 22.

Three tests:
1. test_item_archive_happy_path         — API 204 -> 303 to /items with flash
2. test_item_archive_conflict           — API 409 -> 303 back to detail
3. test_item_archive_button_not_shown   — already-archived item has no archive form

The items detail template shows Edit + Archive buttons only when item.archived_at is falsy.
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

_ITEM_ID = "eeeeeeee-1111-1111-1111-eeeeeeeeeeee"
_ACCOUNT_ID_1 = "cccccccc-1111-1111-1111-cccccccccccc"
_ACCOUNT_ID_2 = "cccccccc-2222-2222-2222-cccccccccccc"
_ACCOUNT_ID_3 = "cccccccc-3333-3333-3333-cccccccccccc"

_MOCK_ITEM_ACTIVE = {
    "id": _ITEM_ID,
    "sku": "ARCH-001",
    "name": "Archivable Widget",
    "item_type": "inventory",
    "description": None,
    "cost_method": "WAC",
    "default_sale_price": "5.00",
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

_MOCK_ITEM_ARCHIVED = {
    **_MOCK_ITEM_ACTIVE,
    "archived_at": "2026-04-24T10:00:00Z",
    "version": 2,
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Archive happy path — API 204 -> 303 to /items with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /items/{id}/archive; API 204 -> 303 to /items."""
    respx_mock.delete(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect).
    respx_mock.get(f"{_API_BASE}/api/v1/items").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 200, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/items/{_ITEM_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/items"


# ---------------------------------------------------------------------------
# 2. Archive conflict — API 409 -> 303 back to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 redirect back to item detail."""
    respx_mock.delete(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/items/{_ITEM_ID}/archive",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/items/{_ITEM_ID}"


# ---------------------------------------------------------------------------
# 3. Archive button NOT shown when item is already archived
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_item_archive_button_not_shown(respx_mock: respx.MockRouter) -> None:
    """Detail page for an already-archived item must not show the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/items/{_ITEM_ID}").mock(
        return_value=Response(200, json=_MOCK_ITEM_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/items/{_ITEM_ID}")

    assert resp.status_code == 200
    # Archive form must not be shown for an already-archived item.
    assert f"/items/{_ITEM_ID}/archive" not in resp.text
    # Edit button also not shown.
    assert f"/items/{_ITEM_ID}/edit" not in resp.text
