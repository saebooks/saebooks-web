"""Tests for the invoice archive action — Lane D cycle 20.

Four tests:
1. test_invoice_archive_happy_path     — POST /invoices/{id}/archive; API 204 -> 303 to /invoices with flash
2. test_invoice_archive_conflict       — API 409 -> 303 back to detail with conflict flash
3. test_invoice_archive_gate_failure   — API 422 (POSTED gate) -> 303 back to detail with API flash
4. test_invoice_archive_button_hidden  — detail page for POSTED invoice has no archive form
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

_INVOICE_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"

_MOCK_INVOICE_DRAFT = {
    "id": _INVOICE_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0001",
    "issue_date": "2026-04-23",
    "due_date": "2026-05-23",
    "status": "DRAFT",
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "notes": None,
    "payment_terms": "Net 30",
    "posted_at": None,
    "posted_by": None,
    "version": 3,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_INVOICE_POSTED = {**_MOCK_INVOICE_DRAFT, "status": "POSTED", "version": 4}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_contacts_accounts_taxcodes(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )


# ---------------------------------------------------------------------------
# 1. Happy path — 204 -> 303 to list with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/{id}/archive; API 204 -> 303 redirect to /invoices with flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect) — mock the invoices list API call
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/archive",
            data={"version": "3"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/invoices"


# ---------------------------------------------------------------------------
# 2. Conflict — 409 -> 303 back to detail with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/archive",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 3. Gate failure — API 422 -> 303 back to detail with API flash message
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_archive_gate_failure(respx_mock: respx.MockRouter) -> None:
    """API returns 422 (e.g. POSTED invoice) -> 303 back to detail with API message."""
    respx_mock.delete(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(
            422, json={"detail": "Cannot archive a POSTED invoice."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/archive",
            data={"version": "4"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 4. Archive button absent in detail when status is POSTED
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_archive_button_hidden_when_posted(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a POSTED invoice must not render the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    # Archive form must not be present for POSTED invoices.
    assert f"/invoices/{_INVOICE_ID}/archive" not in resp.text
    # Edit button must also be absent (same DRAFT guard).
    assert f"/invoices/{_INVOICE_ID}/edit" not in resp.text
