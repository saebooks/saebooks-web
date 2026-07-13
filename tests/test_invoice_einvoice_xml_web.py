"""Tests for the EE e-invoice XML surface (EN 16931 / Peppol BIS 3.0).

Covers the two pieces that wire up the engine's
``GET /api/v1/invoices/{id}/einvoice.xml`` contract:

  * ``GET /invoices/{id}/einvoice.xml`` — the thin web proxy: auth guard,
    happy-path round-trip (stubbed engine XML + filename), and the 422
    problem+json path where the generator's ``detail`` is surfaced to the
    user as a flash rather than a bare 500.
  * The EE-gated "Download e-invoice (XML)" action on the invoice detail
    page — shown for an EE company, absent for an AU company.

Every engine call is mocked at the HTTP boundary with respx, exactly like
``test_invoice_pdf_ee.py``. Jurisdiction is resolved by
CompanyContextMiddleware off the companies + tax_codes calls, so those are
stubbed via the shared ``_mock_companies`` / ``_mock_tax_codes`` helpers.
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
from tests.test_jurisdiction_gating import (
    _AU_COMPANY,
    _EE_COMPANY,
    _mock_companies,
    _mock_tax_codes,
)

_INV_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTACT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_API_BASE = settings.api_url.rstrip("/")
_FAKE_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Invoice xmlns="urn:oasis:names:tc:ubl:schema:xsd:Invoice-2">'
    b"<ID>INV-0042</ID></Invoice>"
)


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-einvoice"})

_MOCK_INVOICE = {
    "id": _INV_ID,
    "company_id": "11111111-1111-1111-1111-111111111111",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0042",
    "issue_date": "2026-04-01",
    "due_date": "2026-04-30",
    "status": "POSTED",
    "subtotal": "1000.00",
    "tax_total": "200.00",
    "total": "1200.00",
    "amount_paid": "0.00",
    "currency": "EUR",
    "fx_rate": "1.0",
    "notes": None,
    "flagged_for_review": False,
    "review_note": None,
    "stripe_payment_link": None,
    "version": 1,
    "lines": [],
}


def _client(follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=follow_redirects,
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


def _mock_context(respx_mock: respx.MockRouter, *, jurisdiction: str) -> None:
    """Stub the middleware's jurisdiction-resolution calls."""
    company = _EE_COMPANY if jurisdiction == "EE" else _AU_COMPANY
    _mock_companies(respx_mock, company)
    _mock_tax_codes(respx_mock, jurisdiction)


def _mock_detail(respx_mock: respx.MockRouter, *, jurisdiction: str) -> None:
    """Stub everything the invoice detail page fetches."""
    _mock_context(respx_mock, jurisdiction=jurisdiction)
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )


# ---------------------------------------------------------------------------
# Download route — GET /invoices/{id}/einvoice.xml
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_einvoice_auth_required() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get(f"/invoices/{_INV_ID}/einvoice.xml")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
@respx.mock
async def test_einvoice_download_round_trips(respx_mock: respx.MockRouter) -> None:
    """EE company: the proxy streams the engine's XML through with the
    engine's own filename and an application/xml content type."""
    _mock_context(respx_mock, jurisdiction="EE")
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}/einvoice.xml").mock(
        return_value=Response(
            200,
            content=_FAKE_XML,
            headers={
                "content-type": "application/xml",
                "content-disposition": 'inline; filename="invoice-INV-0042.xml"',
            },
        )
    )

    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/einvoice.xml")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")
    assert resp.headers["content-disposition"] == 'inline; filename="invoice-INV-0042.xml"'
    assert resp.content == _FAKE_XML


@pytest.mark.asyncio
@respx.mock
async def test_einvoice_404_when_engine_404(respx_mock: respx.MockRouter) -> None:
    _mock_context(respx_mock, jurisdiction="EE")
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}/einvoice.xml").mock(
        return_value=Response(404, json={"detail": "Invoice not found"})
    )
    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/einvoice.xml")
    assert resp.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_einvoice_422_surfaces_generator_detail_as_flash(
    respx_mock: respx.MockRouter,
) -> None:
    """A 422 generator refusal (problem+json) must land as a redirect +
    flash carrying the engine's own message — never a 500, never generic."""
    detail = "Invoice must be posted before an e-invoice can be issued."
    _mock_context(respx_mock, jurisdiction="EE")
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}/einvoice.xml").mock(
        return_value=Response(
            422,
            json={
                "type": "about:blank",
                "title": "Unprocessable Entity",
                "status": 422,
                "code": "validation_failed",
                "detail": detail,
            },
        )
    )
    # The redirect target renders the flash — stub the detail page too so we
    # can follow through and assert the message actually reaches the user.
    _mock_detail(respx_mock, jurisdiction="EE")

    async with _client(follow_redirects=False) as client:
        resp = await client.get(f"/invoices/{_INV_ID}/einvoice.xml")
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/invoices/{_INV_ID}"
        followed = await client.get(f"/invoices/{_INV_ID}")

    assert followed.status_code == 200
    assert detail in followed.text


# ---------------------------------------------------------------------------
# EE-gated button on the invoice detail page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_download_action_shown_for_ee_company(respx_mock: respx.MockRouter) -> None:
    _mock_detail(respx_mock, jurisdiction="EE")
    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}")
    assert resp.status_code == 200
    assert f"/invoices/{_INV_ID}/einvoice.xml" in resp.text
    assert "Download e-invoice (XML)" in resp.text


@pytest.mark.asyncio
@respx.mock
async def test_download_action_hidden_for_au_company(respx_mock: respx.MockRouter) -> None:
    _mock_detail(respx_mock, jurisdiction="AU")
    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}")
    assert resp.status_code == 200
    assert f"/invoices/{_INV_ID}/einvoice.xml" not in resp.text
    assert "Download e-invoice (XML)" not in resp.text
