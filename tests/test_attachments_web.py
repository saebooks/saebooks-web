"""Smoke tests for the attachments panel — Phase 1.5.

Six tests covering:
1. Invoice detail renders the Attachments section when vault is enabled + empty.
2. Invoice detail renders attachment filename when list is non-empty.
3. Invoice detail renders disabled card when vault returns 503.
4. POST /attachments uploads and re-renders the panel (HTMX swap).
5. DELETE /attachments/{id} deletes and re-renders the panel.
6. Bill detail renders the Attachments section when vault is enabled.
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
# Helpers / fixtures
# ---------------------------------------------------------------------------

_INVOICE_ID = "11111111-1111-1111-1111-111111111111"
_BILL_ID = "22222222-2222-2222-2222-222222222222"
_FILE_ID = "33333333-3333-3333-3333-333333333333"
_CONTACT_ID = "44444444-4444-4444-4444-444444444444"
_API_BASE = settings.api_url.rstrip("/")

_MOCK_INVOICE = {
    "id": _INVOICE_ID,
    "company_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0042",
    "issue_date": "2026-05-01",
    "due_date": "2026-05-31",
    "status": "POSTED",
    "subtotal": "1000.00",
    "tax_total": "100.00",
    "total": "1100.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "notes": None,
    "payment_terms": "Net 30",
    "stripe_payment_link": None,
    "posted_at": "2026-05-01T10:00:00Z",
    "posted_by": "api:testuser",
    "version": 1,
    "created_at": "2026-05-01T09:00:00Z",
    "updated_at": "2026-05-01T10:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_BILL = {
    "id": _BILL_ID,
    "company_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "BILL-0007",
    "issue_date": "2026-05-01",
    "due_date": "2026-05-31",
    "status": "POSTED",
    "subtotal": "500.00",
    "tax_total": "50.00",
    "total": "550.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "notes": None,
    "payment_terms": "Net 30",
    "posted_at": "2026-05-01T10:00:00Z",
    "posted_by": "api:testuser",
    "version": 1,
    "created_at": "2026-05-01T09:00:00Z",
    "updated_at": "2026-05-01T10:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_ATTACHMENT = {
    "id": _FILE_ID,
    "filename": "receipt.pdf",
    "content_type": "application/pdf",
    "size": 12345,
    "sha256": "deadbeef",
    "uploaded_by": "saebooks:richard@saee.com.au",
    "uploaded_at": "2026-05-08T01:00:00Z",
    "archived_at": None,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc", "locale": "en"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_attachments_empty(respx_mock: respx.MockRouter) -> None:
    """Invoice detail page renders the Attachments section when list is empty."""
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
    assert "Attachments" in resp.text
    assert "No attachments yet" in resp.text
    # Upload form should be present.
    assert 'name="file"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_attachments_with_file(respx_mock: respx.MockRouter) -> None:
    """Invoice detail page renders the filename when an attachment exists."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[_MOCK_ATTACHMENT])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    assert "receipt.pdf" in resp.text
    # Size should be humanised.
    assert "kB" in resp.text or "12" in resp.text
    # Download button.
    assert f"/attachments/{_FILE_ID}/download" in resp.text
    # Delete button via HTMX.
    assert "hx-delete" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_vault_disabled(respx_mock: respx.MockRouter) -> None:
    """Invoice detail page renders the disabled card on 503 from vault."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(503, json={"detail": "vault disabled"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    # Disabled card text.
    assert "disabled for this instance" in resp.text
    # No upload form when disabled.
    assert 'name="file"' not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_attachment_upload_rerenders_panel(respx_mock: respx.MockRouter) -> None:
    """POST /attachments returns the refreshed panel partial after a successful upload."""
    respx_mock.post(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(201, json=_MOCK_ATTACHMENT)
    )
    # Re-fetch after upload returns list with the new file.
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[_MOCK_ATTACHMENT])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/attachments",
            files={"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"entity_kind": "invoice", "entity_id": _INVOICE_ID},
        )

    assert resp.status_code == 200
    # The returned fragment is the panel div.
    assert f'id="attachments-invoice-{_INVOICE_ID}"' in resp.text
    assert "receipt.pdf" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_attachment_delete_rerenders_panel(respx_mock: respx.MockRouter) -> None:
    """DELETE /attachments/{id} returns the refreshed panel after deletion."""
    respx_mock.delete(f"{_API_BASE}/api/v1/attachments/{_FILE_ID}").mock(
        return_value=Response(204)
    )
    # After deletion the list is empty.
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.delete(
            f"/attachments/{_FILE_ID}",
            params={"entity_kind": "invoice", "entity_id": _INVOICE_ID},
        )

    assert resp.status_code == 200
    assert f'id="attachments-invoice-{_INVOICE_ID}"' in resp.text
    assert "No attachments yet" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bill_detail_attachments_empty(respx_mock: respx.MockRouter) -> None:
    """Bill detail page renders the Attachments section when vault is enabled + empty."""
    respx_mock.get(f"{_API_BASE}/api/v1/bills/{_BILL_ID}").mock(
        return_value=Response(200, json=_MOCK_BILL)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bills/{_BILL_ID}")

    assert resp.status_code == 200
    assert "BILL-0007" in resp.text
    assert "Attachments" in resp.text
    assert "No attachments yet" in resp.text
