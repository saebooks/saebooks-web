"""Tests for the payments list + detail views — Lane D cycle 4.

Five tests:
1. test_payments_requires_auth          — 303 → /login without session
2. test_payments_list_renders_table     — full-page render contains a table row
3. test_payments_list_partial_htmx      — HX-Request returns fragment (no <html>)
4. test_payments_detail_renders         — detail page shows reference + allocation row
5. test_payments_detail_404_propagates  — upstream 404 → HTTP 404 response
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

_PAYMENT_ID = "11111111-1111-1111-1111-222222222222"
_CONTACT_ID = "33333333-3333-3333-3333-444444444444"
_INVOICE_ID = "55555555-5555-5555-5555-666666666666"
_ALLOC_ID = "77777777-7777-7777-7777-888888888888"

_MOCK_PAYMENT = {
    "id": _PAYMENT_ID,
    "company_id": "aaaaaaaa-aaaa-aaaa-aaaa-bbbbbbbbbbbb",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "bank_account_id": "cccccccc-cccc-cccc-cccc-dddddddddddd",
    "number": "PAY-0001",
    "direction": "INCOMING",
    "method": "eft",
    "status": "POSTED",
    "payment_date": "2026-04-15",
    "amount": "1100.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "base_amount": "1100.00",
    "reference": "TXN-REF-001",
    "notes": None,
    "posted_at": "2026-04-15T10:00:00Z",
    "posted_by": "api:testuser",
    "version": 1,
    "created_at": "2026-04-15T09:00:00Z",
    "updated_at": "2026-04-15T10:00:00Z",
    "archived_at": None,
    "allocations": [
        {
            "id": _ALLOC_ID,
            "payment_id": _PAYMENT_ID,
            "invoice_id": _INVOICE_ID,
            "bill_id": None,
            "credit_note_id": None,
            "amount": "1100.00",
        }
    ],
}

_MOCK_PAYMENTS_RESPONSE = {
    "items": [_MOCK_PAYMENT],
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
async def test_payments_requires_auth() -> None:
    """GET /payments without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/payments")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_payments_list_renders_table(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /payments renders the payment reference in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(200, json=_MOCK_PAYMENTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/payments")

    assert resp.status_code == 200
    # Full page — must contain the outer HTML scaffold.
    assert "<html" in resp.text
    # Payment reference should appear.
    assert "TXN-REF-001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_payments_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /payments with HX-Request header returns the fragment, not a full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(200, json=_MOCK_PAYMENTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/payments",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    # Fragment must NOT contain the full <html> wrapper.
    assert "<html" not in resp.text
    # But it should still contain the payment data.
    assert "TXN-REF-001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_payments_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /payments/{id} renders the reference and the allocation row."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}")

    assert resp.status_code == 200
    assert "TXN-REF-001" in resp.text
    # Allocation row should link to the invoice.
    assert _INVOICE_ID in resp.text


@pytest.mark.anyio
@respx.mock
async def test_payments_detail_404_propagates(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(404, json={"detail": "Payment not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}")

    assert resp.status_code == 404
