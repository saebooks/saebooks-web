"""Tests for the receipts list, create, detail, post, and void views.

Nine tests:
1. test_receipts_requires_auth              — 303 -> /login without session
2. test_receipts_list_renders_row           — full-page render contains a receipt row
3. test_receipts_list_partial_htmx          — HX-Request returns fragment (no <html>)
4. test_receipt_new_requires_auth           — GET /receipts/new without session -> 303
5. test_receipt_new_form_renders            — GET /receipts/new returns 200 with the flat-amount line row
6. test_receipt_create_success_redirects    — POST 201 -> 303 to /receipts/{id}
7. test_receipt_detail_renders              — detail page shows lines with amount (not qty*price)
8. test_receipt_post_transition_redirects   — POST /{id}/post -> 303 with flash
9. test_receipt_void_returns_200_not_204    — POST /{id}/void handles the engine's 200 (not 204) response
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

_RCPT_ID = "11111111-1111-1111-1111-111111111111"
_BANK_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
_TAX_CODE_ID = "44444444-4444-4444-4444-444444444444"
_CONTACT_ID = "55555555-5555-5555-5555-555555555555"

_MOCK_BANK_ACCOUNT = {"id": _BANK_ACCOUNT_ID, "code": "1-1000", "name": "Operating Account"}
_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "code": "6-2000", "name": "Bank Fees", "account_type": "EXPENSE"}
_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}
_MOCK_SUPPLIER = {"id": _CONTACT_ID, "name": "Big Bank Ltd", "contact_type": "SUPPLIER"}

_MOCK_RECEIPT = {
    "id": _RCPT_ID,
    "company_id": "66666666-6666-6666-6666-666666666666",
    "bank_account_id": _BANK_ACCOUNT_ID,
    "contact_id": _CONTACT_ID,
    "number": "RCPT-0001",
    "receipt_date": "2026-06-06",
    "status": "DRAFT",
    "reference": "REF-REFUND-1",
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "reason": "Supplier refund",
    "notes": None,
    "journal_entry_id": None,
    "void_journal_entry_id": None,
    "version": 1,
    "created_at": "2026-06-06T00:00:00Z",
    "updated_at": "2026-06-06T00:00:00Z",
    "lines": [
        {
            "id": "77777777-7777-7777-7777-777777777777",
            "line_no": 1,
            "description": "Bank fee refund",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "amount": "100.00",
            "tax_amount": "10.00",
            "line_total": "110.00",
        }
    ],
}

_MOCK_RECEIPTS_RESPONSE = {"items": [_MOCK_RECEIPT], "total": 1, "limit": 50, "offset": 0}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_MOCK_SUPPLIER], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(200, json={"items": [_MOCK_BANK_ACCOUNT], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [_MOCK_ACCOUNT], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": [_MOCK_TAX_CODE], "total": 1})
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_receipts_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/receipts")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_receipts_list_renders_row(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/receipts").mock(
        return_value=Response(200, json=_MOCK_RECEIPTS_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_MOCK_SUPPLIER], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(200, json={"items": [_MOCK_BANK_ACCOUNT], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/receipts")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "RCPT-0001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_receipts_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/receipts").mock(
        return_value=Response(200, json=_MOCK_RECEIPTS_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/receipts", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "RCPT-0001" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_receipt_new_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/receipts/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_receipt_new_form_renders(respx_mock: respx.MockRouter) -> None:
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/receipts/new")

    assert resp.status_code == 200
    assert 'name="bank_account_id"' in resp.text
    assert 'name="receipt_date"' in resp.text
    # Flat amount field, NOT quantity/unit_price — the receipts line schema
    # diverges from credit_notes/invoices.
    assert 'name="lines[0][amount]"' in resp.text
    assert 'name="lines[0][quantity]"' not in resp.text
    assert 'name="lines[0][unit_price]"' not in resp.text
    assert "1-1000" in resp.text
    assert "Big Bank Ltd" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_receipt_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/receipts").mock(
        return_value=Response(201, json=_MOCK_RECEIPT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/receipts/new",
            data={
                "bank_account_id": _BANK_ACCOUNT_ID,
                "receipt_date": "2026-06-06",
                "contact_id": _CONTACT_ID,
                "reference": "REF-REFUND-1",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Bank fee refund",
                "lines[0][amount]": "100.00",
                "lines[0][tax_code_id]": _TAX_CODE_ID,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/receipts/{_RCPT_ID}"


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_receipt_detail_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/receipts/{_RCPT_ID}").mock(
        return_value=Response(200, json=_MOCK_RECEIPT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_SUPPLIER)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_BANK_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_BANK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/receipts/{_RCPT_ID}")

    assert resp.status_code == 200
    assert "RCPT-0001" in resp.text
    assert "Bank fee refund" in resp.text
    assert "110.00" in resp.text


# ---------------------------------------------------------------------------
# Post / void
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_receipt_post_transition_redirects(respx_mock: respx.MockRouter) -> None:
    posted = {**_MOCK_RECEIPT, "status": "POSTED", "journal_entry_id": "88888888-8888-8888-8888-888888888888"}
    respx_mock.post(f"{_API_BASE}/api/v1/receipts/{_RCPT_ID}/post").mock(
        return_value=Response(200, json=posted)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/receipts/{_RCPT_ID}/post", data={"version": "1"})

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/receipts/{_RCPT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_receipt_void_returns_200_not_204(respx_mock: respx.MockRouter) -> None:
    """Receipts' void endpoint returns 200 with the updated record — NOT the
    204 No Content that credit_notes' void returns. The handler must treat
    200 as success (not require a 204 branch)."""
    voided = {**_MOCK_RECEIPT, "status": "VOIDED", "version": 2}
    respx_mock.post(f"{_API_BASE}/api/v1/receipts/{_RCPT_ID}/void").mock(
        return_value=Response(200, json=voided)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/receipts/{_RCPT_ID}/void", data={"version": "1"})

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/receipts/{_RCPT_ID}"
