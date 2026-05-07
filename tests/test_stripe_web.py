"""Tests for Stripe payment link button on invoice detail — D/56.

Six tests:
1. test_stripe_button_shown_for_posted_no_link
       — GET detail for POSTED invoice with no stripe_payment_link shows button
2. test_stripe_copy_button_shown_when_link_exists
       — GET detail for POSTED invoice with stripe_payment_link shows Copy Link
3. test_stripe_button_not_shown_for_draft
       — GET detail for DRAFT invoice shows no payment-link section
4. test_stripe_payment_link_happy_path
       — POST /invoices/{id}/stripe-payment-link; API 200 -> renders link + Copy Link
5. test_stripe_payment_link_503_not_configured
       — API 503 -> renders "Stripe not configured" banner
6. test_stripe_payment_link_422_not_posted
       — API 422 -> renders "Invoice must be posted with outstanding balance"
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

_INVOICE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"

_MOCK_INVOICE_BASE = {
    "id": _INVOICE_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0099",
    "issue_date": "2026-04-01",
    "due_date": "2026-05-01",
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
    "version": 2,
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-04-01T00:00:00Z",
    "archived_at": None,
    "lines": [],
    "stripe_payment_link": None,
}

_MOCK_INVOICE_DRAFT = {**_MOCK_INVOICE_BASE, "status": "DRAFT"}
_MOCK_INVOICE_POSTED = {**_MOCK_INVOICE_BASE, "status": "POSTED"}
_MOCK_INVOICE_POSTED_WITH_LINK = {
    **_MOCK_INVOICE_POSTED,
    "stripe_payment_link": "https://buy.stripe.com/test_abc123",
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-stripe"})


# ---------------------------------------------------------------------------
# 1. POSTED invoice with no link shows Send Payment Link button
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stripe_button_shown_for_posted_no_link(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /invoices/{id} — POSTED with no stripe_payment_link renders Send Payment Link."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_POSTED)
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
    assert "Send Payment Link" in resp.text
    assert f"/invoices/{_INVOICE_ID}/stripe-payment-link" in resp.text


# ---------------------------------------------------------------------------
# 2. POSTED invoice with existing link shows Copy Link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stripe_copy_button_shown_when_link_exists(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /invoices/{id} — POSTED with stripe_payment_link renders Copy Link."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_POSTED_WITH_LINK)
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
    assert "Copy Link" in resp.text
    assert "https://buy.stripe.com/test_abc123" in resp.text
    # Send Payment Link button must NOT be present when link already exists
    assert "Send Payment Link" not in resp.text


# ---------------------------------------------------------------------------
# 3. DRAFT invoice — no payment link section at all
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stripe_button_not_shown_for_draft(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /invoices/{id} — DRAFT invoice: no Stripe payment link section."""
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
    assert "Send Payment Link" not in resp.text
    assert "stripe-payment-link-area" not in resp.text


# ---------------------------------------------------------------------------
# 4. Happy path — POST generates link, HTMX partial returns Copy Link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stripe_payment_link_happy_path(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /invoices/{id}/stripe-payment-link; API 200 -> partial with URL + Copy Link."""
    _URL = "https://buy.stripe.com/test_live_xyz"
    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/stripe-payment-link"
    ).mock(return_value=Response(200, json={"url": _URL}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/stripe-payment-link"
        )

    assert resp.status_code == 200
    assert _URL in resp.text
    assert "Copy Link" in resp.text


# ---------------------------------------------------------------------------
# 5. API 503 — Stripe not configured
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stripe_payment_link_503_not_configured(
    respx_mock: respx.MockRouter,
) -> None:
    """POST; API 503 -> partial renders 'Stripe not configured' error banner."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/stripe-payment-link"
    ).mock(return_value=Response(503, json={"detail": "Stripe not configured"}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/stripe-payment-link"
        )

    assert resp.status_code == 200
    assert "Stripe not configured" in resp.text


# ---------------------------------------------------------------------------
# 6. API 422 — Invoice must be posted with outstanding balance
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_stripe_payment_link_422_not_posted(
    respx_mock: respx.MockRouter,
) -> None:
    """POST; API 422 -> partial renders 'Invoice must be posted with outstanding balance'."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/stripe-payment-link"
    ).mock(
        return_value=Response(
            422,
            json={"detail": "Invoice must be POSTED with an outstanding balance."},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/stripe-payment-link"
        )

    assert resp.status_code == 200
    assert "Invoice must be posted with outstanding balance" in resp.text
