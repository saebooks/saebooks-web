"""Tests for the credit notes list + detail views — Lane D cycle 5.

Five tests:
1. test_credit_notes_requires_auth          — 303 → /login without session
2. test_credit_notes_list_renders_row       — full-page render contains a table row
3. test_credit_notes_list_partial_htmx      — HX-Request returns fragment (no <html>)
4. test_credit_notes_detail_renders         — detail page: lines table, applied and unapplied cases
5. test_credit_notes_detail_404_propagates  — upstream 404 → HTTP 404 response
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

_CN_ID = "11111111-1111-1111-1111-111111111111"
_INVOICE_ID = "22222222-2222-2222-2222-222222222222"
_CONTACT_ID = "33333333-3333-3333-3333-333333333333"

_MOCK_CN_WITH_INVOICE = {
    "id": _CN_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "CN-000001",
    "issue_date": "2026-04-10",
    "status": "POSTED",
    "original_invoice_id": _INVOICE_ID,
    "subtotal": "500.00",
    "tax_total": "50.00",
    "total": "550.00",
    "amount_allocated": "550.00",
    "reason": "Overcharge correction",
    "notes": "Approved by accounts team",
    "posted_at": "2026-04-10T12:00:00Z",
    "posted_by": "api:testuser",
    "version": 1,
    "created_at": "2026-04-10T11:00:00Z",
    "updated_at": "2026-04-10T12:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "line_no": 1,
            "description": "Consulting adjustment",
            "account_id": "66666666-6666-6666-6666-666666666666",
            "tax_code_id": None,
            "quantity": "5.0",
            "unit_price": "100.00",
            "discount_pct": "0.0",
            "line_subtotal": "500.00",
            "line_tax": "50.00",
            "line_total": "550.00",
        }
    ],
}

_MOCK_CN_UNAPPLIED = {
    **_MOCK_CN_WITH_INVOICE,
    "id": "77777777-7777-7777-7777-777777777777",
    "number": "CN-000002",
    "original_invoice_id": None,
    "amount_allocated": "0.00",
    "reason": None,
    "notes": None,
    "status": "DRAFT",
}

_MOCK_CNS_RESPONSE = {
    "items": [_MOCK_CN_WITH_INVOICE],
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
async def test_credit_notes_requires_auth() -> None:
    """GET /credit-notes without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/credit-notes")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_credit_notes_list_renders_row(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /credit-notes renders the CN number in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes").mock(
        return_value=Response(200, json=_MOCK_CNS_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/credit-notes")

    assert resp.status_code == 200
    # Full page — must contain the outer HTML scaffold.
    assert "<html" in resp.text
    # CN number should appear.
    assert "CN-000001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_credit_notes_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /credit-notes with HX-Request header returns the fragment, not a full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes").mock(
        return_value=Response(200, json=_MOCK_CNS_RESPONSE)
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
            "/credit-notes",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    # Fragment must NOT contain the full <html> wrapper.
    assert "<html" not in resp.text
    # But it should still contain the CN data.
    assert "CN-000001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_credit_notes_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /credit-notes/{id} renders the lines table and applied-to linkage.

    Two sub-cases exercised via separate mock calls in the same test:
    1. Applied CN (original_invoice_id set) — invoice link appears.
    2. Unapplied CN (original_invoice_id None) — "Unapplied" text appears.
    """
    # Case 1: applied CN
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_WITH_INVOICE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}")

    assert resp.status_code == 200
    assert "CN-000001" in resp.text
    # Lines table heading and line description should appear.
    assert "Lines" in resp.text
    assert "Consulting adjustment" in resp.text
    # Applied-to invoice link should appear.
    assert _INVOICE_ID in resp.text

    # Case 2: unapplied CN
    unapplied_id = _MOCK_CN_UNAPPLIED["id"]
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{unapplied_id}").mock(
        return_value=Response(200, json=_MOCK_CN_UNAPPLIED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp2 = await client.get(f"/credit-notes/{unapplied_id}")

    assert resp2.status_code == 200
    assert "CN-000002" in resp2.text
    assert "Unapplied" in resp2.text


@pytest.mark.anyio
@respx.mock
async def test_credit_notes_detail_404_propagates(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(404, json={"detail": "Credit note not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}")

    assert resp.status_code == 404
