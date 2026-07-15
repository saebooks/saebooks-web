"""Tests for the invoices list + detail views — Lane D cycle 2.

Five tests:
1. test_invoices_requires_auth        — 303 → /login without session
2. test_invoices_list_renders_table   — full-page render contains a table row
3. test_invoices_list_partial_htmx   — HX-Request returns fragment (no <html>)
4. test_invoices_detail_renders       — detail page shows invoice number
5. test_invoices_detail_404_propagates — upstream 404 → HTTP 404 response
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

_INVOICE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_MOCK_INVOICE = {
    "id": _INVOICE_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0042",
    "issue_date": "2026-04-01",
    "due_date": "2026-04-30",
    "status": "POSTED",
    "subtotal": "1000.00",
    "tax_total": "100.00",
    "total": "1100.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "notes": None,
    "payment_terms": "Net 30",
    "posted_at": "2026-04-01T10:00:00Z",
    "posted_by": "api:testuser",
    "version": 1,
    "created_at": "2026-04-01T09:00:00Z",
    "updated_at": "2026-04-01T10:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "line_no": 1,
            "description": "Consulting services",
            "account_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "tax_code_id": None,
            "quantity": "10.0",
            "unit_price": "100.00",
            "discount_pct": "0.0",
            "line_subtotal": "1000.00",
            "line_tax": "100.00",
            "line_total": "1100.00",
            "project_id": None,
            "item_id": None,
        }
    ],
}

_MOCK_INVOICES_RESPONSE = {
    "items": [_MOCK_INVOICE],
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
async def test_invoices_requires_auth() -> None:
    """GET /invoices without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/invoices")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_invoices_list_renders_table(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /invoices renders the invoice number in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/invoices")

    assert resp.status_code == 200
    # Full page — must contain the outer HTML scaffold.
    assert "<html" in resp.text
    # Invoice number should appear.
    assert "INV-0042" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoices_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /invoices with HX-Request header returns the fragment, not a full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/invoices",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    # Fragment must NOT contain the full <html> wrapper.
    assert "<html" not in resp.text
    # But it should still contain the invoice data.
    assert "INV-0042" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoices_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /invoices/{id} renders the invoice number on the detail page."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    assert "INV-0042" in resp.text
    assert "Consulting services" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoices_detail_404_propagates(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(404, json={"detail": "Invoice not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 404
