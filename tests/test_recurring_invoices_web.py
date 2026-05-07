"""Tests for the recurring invoices list + detail views — Lane D cycle 26.

Four tests:
1. test_recurring_invoices_list_renders  — full-page GET 200 with name in body
2. test_recurring_invoices_list_htmx     — HX-Request returns fragment (no <html>)
3. test_recurring_invoices_detail_lines  — detail page renders lines table
4. test_recurring_invoices_status_filter — status filter forwarded to upstream API
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

_RI_ID = "bbbbbbbb-bbbb-bbbb-bbbb-000000000001"
_CONTACT_ID = "cccccccc-cccc-cccc-cccc-000000000002"
_ACCOUNT_ID = "dddddddd-dddd-dddd-dddd-000000000004"

_MOCK_LINE = {
    "id": "eeeeeeee-eeee-eeee-eeee-000000000001",
    "line_no": 1,
    "description": "Monthly maintenance fee",
    "account_id": _ACCOUNT_ID,
    "tax_code_id": None,
    "quantity": "1.00",
    "unit_price": "250.00",
    "discount_pct": "0.00",
}

_MOCK_RI = {
    "id": _RI_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "name": "Monthly Retainer — Acme Corp",
    "frequency": "MONTHLY",
    "status": "ACTIVE",
    "anchor_day": 1,
    "next_run": "2026-05-01",
    "end_date": None,
    "last_run": "2026-04-01",
    "due_days": 14,
    "payment_terms": "Net 14",
    "notes": "Standard monthly retainer.",
    "auto_post": False,
    "invoices_generated": 15,
    "version": 2,
    "created_at": "2025-02-01T08:00:00Z",
    "updated_at": "2026-04-01T09:00:00Z",
    "archived_at": None,
    "lines": [_MOCK_LINE],
}

_MOCK_RI_RESPONSE = {
    "items": [_MOCK_RI],
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
@respx.mock
async def test_recurring_invoices_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /recurring-invoices renders the name in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices").mock(
        return_value=Response(200, json=_MOCK_RI_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/recurring-invoices")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Monthly Retainer" in resp.text
    assert "MONTHLY" in resp.text.upper() or "Monthly" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_recurring_invoices_list_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices with HX-Request returns fragment, not full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices").mock(
        return_value=Response(200, json=_MOCK_RI_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/recurring-invoices",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "Monthly Retainer" in resp.text
    assert "recurring-invoices-table" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_recurring_invoices_detail_lines(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices/{id} renders the detail page with line items."""
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(200, json=_MOCK_RI)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/recurring-invoices/{_RI_ID}")

    assert resp.status_code == 200
    assert "Monthly Retainer" in resp.text
    # Line item content
    assert "Monthly maintenance fee" in resp.text
    assert "250" in resp.text
    # Schedule fields
    assert "2026-05-01" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_recurring_invoices_status_filter(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices?status=ACTIVE forwards the status param to the API."""
    route = respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices").mock(
        return_value=Response(200, json=_MOCK_RI_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/recurring-invoices", params={"status": "ACTIVE"})

    assert resp.status_code == 200
    assert route.called
    called_url = str(route.calls[0].request.url)
    assert "status=ACTIVE" in called_url
