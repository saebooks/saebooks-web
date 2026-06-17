"""Tests for the AI document extraction web routes — D/55.

Twelve tests:
1.  test_bills_extract_probe_requires_auth         — GET /bills/extract-document/probe without session -> 303
2.  test_bills_extract_probe_flag_off              — API 404 -> probe returns 404 (button hidden)
3.  test_bills_extract_probe_flag_on               — API 200 -> probe returns 200 with upload button HTML
4.  test_bills_extract_probe_key_unconfigured      — API 503 -> probe returns 503 (button hidden)
5.  test_bills_extract_success_high_confidence     — POST upload -> API 200 high confidence -> fills fields
6.  test_bills_extract_success_low_confidence      — POST upload -> API 200 low confidence -> banner shown
7.  test_bills_extract_api_404_flag_off            — POST upload -> API 404 -> error fragment
8.  test_bills_extract_api_503_key_missing         — POST upload -> API 503 -> error fragment
9.  test_bills_extract_no_file                     — POST without file -> 400 error fragment
10. test_invoices_extract_probe_flag_on            — GET /invoices/extract-document/probe -> 200
11. test_invoices_extract_success_high_confidence  — POST upload to invoice route -> fills fields
12. test_bills_new_page_contains_probe_hx_attrs    — GET /bills/new includes hx-get probe div
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
# Constants / helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"

_MOCK_CONTACTS = {"items": [{"id": _CONTACT_ID, "name": "Acme Supplies Pty Ltd"}], "total": 1}
_MOCK_ACCOUNTS = {"items": [{"id": _ACCOUNT_ID, "name": "Office Expenses", "code": "6100"}], "total": 1}
_MOCK_TAX_CODES = {"items": [{"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}], "total": 1}

_EXTRACTION_HIGH = {
    "vendor_name": "Acme Supplies Pty Ltd",
    "invoice_number": "INV-0042",
    "date": "2026-04-20",
    "due_date": "2026-05-20",
    "subtotal": "200.00",
    "tax_amount": "20.00",
    "total": "220.00",
    "currency": "AUD",
    "line_items": [
        {"description": "Office supplies", "quantity": 2, "unit_price": 100.00}
    ],
    "notes": "Thank you for your business.",
    "extraction_confidence": 0.92,
}

_EXTRACTION_LOW = {
    "vendor_name": "Unknown Vendor",
    "invoice_number": "???",
    "date": "2026-04-01",
    "due_date": None,
    "subtotal": "50.00",
    "tax_amount": "5.00",
    "total": "55.00",
    "currency": "AUD",
    "line_items": [],
    "notes": None,
    "extraction_confidence": 0.45,
}

_FAKE_PDF = b"%PDF-1.4 fake content for testing"


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-ai"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    """Mock the three dropdown API calls used by the bills/new form."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json=_MOCK_CONTACTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )


# ---------------------------------------------------------------------------
# 1. Probe requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bills_extract_probe_requires_auth() -> None:
    """GET /bills/extract-document/probe without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/bills/extract-document/probe")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Probe — flag off (API 404)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_probe_flag_off(respx_mock: respx.MockRouter) -> None:
    """GET /bills/extract-document/probe when API returns 404 -> probe returns 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/extract-document/probe")

    assert resp.status_code == 404
    # No upload button rendered when flag is off.
    assert "extract-document" not in resp.text


# ---------------------------------------------------------------------------
# 3. Probe — flag on (API returns non-404/503)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_probe_flag_on(respx_mock: respx.MockRouter) -> None:
    """GET /bills/extract-document/probe when feature is enabled -> 200 with upload button."""
    # API may return 405 (method not allowed on GET) or any non-404/503 status —
    # either way the feature is considered enabled.
    respx_mock.get(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(405, json={"detail": "Method Not Allowed"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/extract-document/probe")

    assert resp.status_code == 200
    # Upload panel HTML is returned.
    assert "ai-extract-panel" in resp.text
    assert "Extract from document" in resp.text


# ---------------------------------------------------------------------------
# 4. Probe — API key not configured (503)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_probe_key_unconfigured(respx_mock: respx.MockRouter) -> None:
    """GET /bills/extract-document/probe when API returns 503 -> probe returns 503."""
    respx_mock.get(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(503, json={"detail": "AI service not configured"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/extract-document/probe")

    assert resp.status_code == 503
    assert "Extract from document" not in resp.text


# ---------------------------------------------------------------------------
# 5. POST extract — high confidence fills fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_success_high_confidence(respx_mock: respx.MockRouter) -> None:
    """POST /bills/extract-document with high-confidence result -> fields JS + badge."""
    respx_mock.post(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(200, json=_EXTRACTION_HIGH)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/extract-document",
            files={"document": ("invoice.pdf", _FAKE_PDF, "application/pdf")},
        )

    assert resp.status_code == 200
    # Confidence badge present.
    assert "AI confidence: 92%" in resp.text
    # No low-confidence warning.
    assert "Low confidence" not in resp.text
    # JS to fill fields is present.
    assert "setVal" in resp.text
    assert "2026-04-20" in resp.text  # date
    assert "INV-0042" in resp.text    # invoice_number mapped to supplier_reference


# ---------------------------------------------------------------------------
# 6. POST extract — low confidence shows banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_success_low_confidence(respx_mock: respx.MockRouter) -> None:
    """POST /bills/extract-document with low-confidence result -> banner shown."""
    respx_mock.post(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(200, json=_EXTRACTION_LOW)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/extract-document",
            files={"document": ("scan.png", _FAKE_PDF, "image/png")},
        )

    assert resp.status_code == 200
    assert "Low confidence" in resp.text
    assert "AI confidence: 45%" in resp.text


# ---------------------------------------------------------------------------
# 7. POST extract — API 404 (flag off) -> error fragment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_api_404_flag_off(respx_mock: respx.MockRouter) -> None:
    """POST /bills/extract-document when API returns 404 -> error message shown."""
    respx_mock.post(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/extract-document",
            files={"document": ("invoice.pdf", _FAKE_PDF, "application/pdf")},
        )

    assert resp.status_code == 404
    assert "not enabled" in resp.text.lower()


# ---------------------------------------------------------------------------
# 8. POST extract — API 503 (key missing) -> error fragment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_extract_api_503_key_missing(respx_mock: respx.MockRouter) -> None:
    """POST /bills/extract-document when API returns 503 -> clear error message."""
    respx_mock.post(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(503, json={"detail": "AI service not configured"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/extract-document",
            files={"document": ("invoice.pdf", _FAKE_PDF, "application/pdf")},
        )

    assert resp.status_code == 503
    assert "API key" in resp.text or "not available" in resp.text.lower()


# ---------------------------------------------------------------------------
# 9. POST extract — no file provided -> 400
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bills_extract_no_file() -> None:
    """POST /bills/extract-document without a file -> 400 error fragment."""
    from conftest import TEST_CSRF_TOKEN
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/extract-document",
            data={"csrf_token": TEST_CSRF_TOKEN},
        )

    assert resp.status_code == 400
    assert "No file" in resp.text


# ---------------------------------------------------------------------------
# 10. Invoices probe — flag on
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoices_extract_probe_flag_on(respx_mock: respx.MockRouter) -> None:
    """GET /invoices/extract-document/probe when feature is enabled -> 200."""
    respx_mock.get(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(405, json={"detail": "Method Not Allowed"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/invoices/extract-document/probe")

    assert resp.status_code == 200
    assert "ai-extract-panel" in resp.text
    # form_context is "invoice" -> action URL should reference invoices
    assert "invoices/extract-document" in resp.text


# ---------------------------------------------------------------------------
# 11. Invoices POST extract — high confidence
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoices_extract_success_high_confidence(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/extract-document with high-confidence result fills fields."""
    respx_mock.post(f"{_API_BASE}/api/v1/documents/extract").mock(
        return_value=Response(200, json=_EXTRACTION_HIGH)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/invoices/extract-document",
            files={"document": ("invoice.pdf", _FAKE_PDF, "application/pdf")},
        )

    assert resp.status_code == 200
    assert "AI confidence: 92%" in resp.text
    # For invoices, invoice_number maps to 'number' field, not supplier_reference.
    assert "INV-0042" in resp.text
    # The JS setVal for invoices uses 'number', not 'supplier_reference'.
    assert "supplier_reference" not in resp.text


# ---------------------------------------------------------------------------
# 12. bills/new page contains the probe hx-get div
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_new_page_contains_probe_hx_attrs(respx_mock: respx.MockRouter) -> None:
    """GET /bills/new should include the probe div with correct hx-get attribute."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/new")

    assert resp.status_code == 200
    assert "hx-get" in resp.text
    assert "/bills/extract-document/probe" in resp.text
    assert "ai-extract-probe-target" in resp.text
