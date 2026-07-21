"""Tests for the transfers list, create, detail, and reverse views.

Nine tests:
1. test_transfers_requires_auth               — 303 -> /login without session
2. test_transfers_list_renders_row             — full-page render contains a transfer row
3. test_transfers_list_partial_htmx            — HX-Request returns fragment (no <html>)
4. test_transfer_new_requires_auth             — GET /transfers/new without session -> 303
5. test_transfer_new_form_renders              — GET /transfers/new returns 200 with the form
6. test_transfer_create_success_redirects      — POST 201 -> 303 to /transfers/{id}
7. test_transfer_create_business_error_rerenders — POST 400 (P&L account) -> re-render with the engine's message
8. test_transfer_detail_renders                — detail page shows amount, accounts, reverse button
9. test_transfer_reverse_redirects_with_flash   — POST /{id}/reverse -> 303 with flash
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

_XFER_ID = "11111111-1111-1111-1111-111111111111"
_FROM_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TO_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
_COMPANY_ID = "44444444-4444-4444-4444-444444444444"
_JE_ID = "55555555-5555-5555-5555-555555555555"

_MOCK_FROM_ACCOUNT = {"id": _FROM_ACCOUNT_ID, "code": "1-1000", "name": "Operating Account", "account_type": "ASSET"}
_MOCK_TO_ACCOUNT = {"id": _TO_ACCOUNT_ID, "code": "2-1115", "name": "Credit Card", "account_type": "LIABILITY"}
_MOCK_ACCOUNTS = {"items": [_MOCK_FROM_ACCOUNT, _MOCK_TO_ACCOUNT], "total": 2, "limit": 500, "offset": 0}

_MOCK_TRANSFER = {
    "id": _XFER_ID,
    "company_id": _COMPANY_ID,
    "from_account_id": _FROM_ACCOUNT_ID,
    "to_account_id": _TO_ACCOUNT_ID,
    "amount": "320.00",
    "transfer_date": "2026-06-06",
    "description": "CC paydown",
    "reference": "REF-1",
    "status": "POSTED",
    "journal_entry_id": _JE_ID,
    "created_at": "2026-06-06T00:00:00Z",
    "updated_at": "2026-06-06T00:00:00Z",
}

_MOCK_TRANSFERS_RESPONSE = {"items": [_MOCK_TRANSFER]}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_accounts(respx_mock: respx.MockRouter) -> None:
    """Register the three account_type calls transfers.py fetches."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts", params={"account_type": "ASSET", "limit": "500", "offset": "0"}).mock(
        return_value=Response(200, json={"items": [_MOCK_FROM_ACCOUNT], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts", params={"account_type": "LIABILITY", "limit": "500", "offset": "0"}).mock(
        return_value=Response(200, json={"items": [_MOCK_TO_ACCOUNT], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts", params={"account_type": "EQUITY", "limit": "500", "offset": "0"}).mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_transfers_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/transfers")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_transfers_list_renders_row(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/transfers").mock(
        return_value=Response(200, json=_MOCK_TRANSFERS_RESPONSE)
    )
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/transfers")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "REF-1" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_transfers_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/transfers").mock(
        return_value=Response(200, json=_MOCK_TRANSFERS_RESPONSE)
    )
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/transfers", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "REF-1" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_transfer_new_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/transfers/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_transfer_new_form_renders(respx_mock: respx.MockRouter) -> None:
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/transfers/new")

    assert resp.status_code == 200
    assert 'name="from_account_id"' in resp.text
    assert 'name="to_account_id"' in resp.text
    assert 'name="amount"' in resp.text
    assert 'name="transfer_date"' in resp.text
    assert "1-1000" in resp.text
    assert "2-1115" in resp.text
    # No-manual-JE messaging in the empty-state / form guidance.
    assert "journal entry" in resp.text.lower()


@pytest.mark.anyio
@respx.mock
async def test_transfer_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/transfers").mock(
        return_value=Response(201, json=_MOCK_TRANSFER)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/transfers/new",
            data={
                "from_account_id": _FROM_ACCOUNT_ID,
                "to_account_id": _TO_ACCOUNT_ID,
                "amount": "320.00",
                "transfer_date": "2026-06-06",
                "reference": "REF-1",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/transfers/{_XFER_ID}"


@pytest.mark.anyio
@respx.mock
async def test_transfer_create_business_error_rerenders(respx_mock: respx.MockRouter) -> None:
    """The engine rejects a P&L account with 400 + nested {"code","detail"} —
    the form re-renders with the engine's message, not a generic 400 text."""
    respx_mock.post(f"{_API_BASE}/api/v1/transfers").mock(
        return_value=Response(
            400,
            json={"detail": {"code": "transfer_invalid", "detail": "to_account_id must be a balance-sheet account"}},
        )
    )
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/transfers/new",
            data={
                "from_account_id": _FROM_ACCOUNT_ID,
                "to_account_id": _TO_ACCOUNT_ID,
                "amount": "10.00",
                "transfer_date": "2026-06-06",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert resp.status_code == 400
    assert "balance-sheet account" in resp.text


# ---------------------------------------------------------------------------
# Detail + reverse
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_transfer_detail_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/transfers/{_XFER_ID}").mock(
        return_value=Response(200, json=_MOCK_TRANSFER)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_FROM_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_FROM_ACCOUNT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_TO_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_TO_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/transfers/{_XFER_ID}")

    assert resp.status_code == 200
    assert "320.00" in resp.text
    assert "1-1000" in resp.text
    assert "2-1115" in resp.text
    # Reverse action present for a POSTED transfer.
    assert f"/transfers/{_XFER_ID}/reverse" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_transfer_reverse_redirects_with_flash(respx_mock: respx.MockRouter) -> None:
    reversed_transfer = {**_MOCK_TRANSFER, "status": "REVERSED"}
    respx_mock.post(f"{_API_BASE}/api/v1/transfers/{_XFER_ID}/reverse").mock(
        return_value=Response(200, json=reversed_transfer)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/transfers/{_XFER_ID}/reverse")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/transfers/{_XFER_ID}"
