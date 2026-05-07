"""Tests for invoice POST and VOID transition actions — Lane D cycle 25.

Six tests:
1. test_invoice_post_happy_path          — POST /invoices/{id}/post; API 200 -> 303 to detail with flash
2. test_invoice_post_conflict            — API 409 -> 303 back to detail with conflict flash
3. test_invoice_post_validation_error    — API 422 -> 303 back to detail with API error flash
4. test_invoice_void_happy_path          — POST /invoices/{id}/void; API 200 -> 303 to detail with flash
5. test_invoice_void_on_draft_422        — void a DRAFT invoice -> API 422 -> 303 with flash
6. test_invoice_void_button_not_shown_for_draft — void button absent on DRAFT invoice detail
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


# ---------------------------------------------------------------------------
# 1. Happy path — POST /invoices/{id}/post; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_post_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/{id}/post; API 200 -> 303 redirect to detail with 'Invoice posted.' flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/post").mock(
        return_value=Response(200, json=_MOCK_INVOICE_POSTED)
    )
    # Detail GET (after redirect follows) — needed if follow_redirects=True.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/post",
            data={"version": "3"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 2. Conflict — API 409 -> 303 back to detail with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_post_conflict(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/post").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/post",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"

    # Flash message must carry the conflict text.  Follow the redirect and check.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_DRAFT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/invoices/{_INVOICE_ID}/post",
            data={"version": "2"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 3. Validation error — API 422 -> 303 back to detail with API error message
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_post_validation_error(respx_mock: respx.MockRouter) -> None:
    """API 422 (e.g. business rule) -> 303 back to detail with API message as flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/post").mock(
        return_value=Response(
            422, json={"detail": "Invoice has no lines and cannot be posted."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/post",
            data={"version": "3"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"

    # Follow the redirect and check the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_DRAFT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/invoices/{_INVOICE_ID}/post",
            data={"version": "3"},
        )
    assert "Invoice has no lines" in resp2.text


# ---------------------------------------------------------------------------
# 4. Happy path — void; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_void_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/{id}/void; API 200 -> 303 redirect to detail with 'Invoice voided.' flash."""
    _MOCK_INVOICE_VOIDED = {**_MOCK_INVOICE_POSTED, "status": "VOIDED", "version": 5}

    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/void").mock(
        return_value=Response(200, json=_MOCK_INVOICE_VOIDED)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_VOIDED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/void",
            data={"version": "4"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 5. Void a DRAFT invoice — API 422 pass-through -> 303 with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_void_on_draft_422(respx_mock: respx.MockRouter) -> None:
    """Voiding a DRAFT invoice: API returns 422 -> 303 back to detail with flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/void").mock(
        return_value=Response(
            422, json={"detail": "Only POSTED invoices can be voided."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/void",
            data={"version": "3"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"

    # Follow and verify the message propagates.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_DRAFT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/invoices/{_INVOICE_ID}/void",
            data={"version": "3"},
        )
    assert "Only POSTED" in resp2.text


# ---------------------------------------------------------------------------
# 6. Void button not shown on DRAFT invoice detail page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_void_button_not_shown_for_draft(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a DRAFT invoice must not render the void form."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_DRAFT)
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
    # Void form must not be present for DRAFT invoices.
    assert f"/invoices/{_INVOICE_ID}/void" not in resp.text
    # Post button MUST be present for DRAFT invoices.
    assert f"/invoices/{_INVOICE_ID}/post" in resp.text
