"""Tests for bill POST and VOID transition actions — Lane D cycle 25.

Four tests:
1. test_bill_post_happy_path      — POST /bills/{id}/post; API 200 -> 303 to detail with flash
2. test_bill_post_conflict        — API 409 -> 303 back to detail with conflict flash
3. test_bill_void_happy_path      — POST /bills/{id}/void; API 200 -> 303 to detail with flash
4. test_bill_void_button_not_shown_for_draft — void button absent on DRAFT bill detail
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

_BILL_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"

_MOCK_BILL_DRAFT = {
    "id": _BILL_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "BILL-0001",
    "supplier_reference": None,
    "issue_date": "2026-04-23",
    "due_date": "2026-05-23",
    "status": "DRAFT",
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "notes": None,
    "version": 2,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_BILL_POSTED = {**_MOCK_BILL_DRAFT, "status": "POSTED", "version": 3}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Happy path — POST /bills/{id}/post; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_post_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /bills/{id}/post; API 200 -> 303 redirect to detail with 'Bill posted.' flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/bills/{_BILL_ID}/post").mock(
        return_value=Response(200, json=_MOCK_BILL_POSTED)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bills/{_BILL_ID}").mock(
        return_value=Response(200, json=_MOCK_BILL_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bills/{_BILL_ID}/post",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"


# ---------------------------------------------------------------------------
# 2. Conflict — API 409 -> 303 back to detail with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_post_conflict(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/bills/{_BILL_ID}/post").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bills/{_BILL_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"

    # Follow the redirect and check the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/bills/{_BILL_ID}").mock(
        return_value=Response(200, json=_MOCK_BILL_DRAFT)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/bills/{_BILL_ID}/post",
            data={"version": "1"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 3. Happy path — void; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_void_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /bills/{id}/void; API 200 -> 303 redirect to detail with 'Bill voided.' flash."""
    _MOCK_BILL_VOIDED = {**_MOCK_BILL_POSTED, "status": "VOIDED", "version": 4}

    respx_mock.post(f"{_API_BASE}/api/v1/bills/{_BILL_ID}/void").mock(
        return_value=Response(200, json=_MOCK_BILL_VOIDED)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bills/{_BILL_ID}").mock(
        return_value=Response(200, json=_MOCK_BILL_VOIDED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bills/{_BILL_ID}/void",
            data={"version": "3"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"


# ---------------------------------------------------------------------------
# 4. Void button not shown on DRAFT bill detail page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_void_button_not_shown_for_draft(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a DRAFT bill must not render the void form."""
    respx_mock.get(f"{_API_BASE}/api/v1/bills/{_BILL_ID}").mock(
        return_value=Response(200, json=_MOCK_BILL_DRAFT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bills/{_BILL_ID}")

    assert resp.status_code == 200
    # Void form must not be present for DRAFT bills.
    assert f"/bills/{_BILL_ID}/void" not in resp.text
    # Post button MUST be present for DRAFT bills.
    assert f"/bills/{_BILL_ID}/post" in resp.text
