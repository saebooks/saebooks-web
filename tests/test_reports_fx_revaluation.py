"""Tests for the FX revaluation report HTML view — Lane D cycle 32.

Four tests:
1. test_fx_revaluation_get_200         — full-page GET 200, FX items rendered
2. test_fx_revaluation_htmx_partial    — HX-Request returns fragment (no <html>)
3. test_fx_revaluation_as_of_date_param — as_of_date query param accepted, no error
4. test_fx_revaluation_empty_items     — empty items list shows no-data message
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
# Helpers
# ---------------------------------------------------------------------------


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")

# ---------------------------------------------------------------------------
# Mock API response fixtures
# ---------------------------------------------------------------------------

_FX_REPORT = {
    "as_of_date": "2026-04-24",
    "base_currency": "AUD",
    "items": [
        {
            "entity_type": "INVOICE",
            "entity_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "entity_ref": "INV-000001",
            "contact_name": "Acme Corp",
            "currency": "USD",
            "original_amount": 1000.0,
            "amount_paid": 0.0,
            "outstanding_foreign": 1000.0,
            "outstanding_base": None,
            "note": "FX rate not available — manual revaluation required",
        },
        {
            "entity_type": "BILL",
            "entity_id": "bbbb0002-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "entity_ref": "BILL-000042",
            "contact_name": "Overseas Supplier",
            "currency": "EUR",
            "original_amount": 500.0,
            "amount_paid": 200.0,
            "outstanding_foreign": 300.0,
            "outstanding_base": None,
            "note": "FX rate not available — manual revaluation required",
        },
    ],
    "total_items": 2,
    "note": "Live FX rates not configured. Amounts shown in original currency.",
}

_FX_REPORT_EMPTY = {
    "as_of_date": "2026-01-01",
    "base_currency": "AUD",
    "items": [],
    "total_items": 0,
    "note": "Live FX rates not configured. Amounts shown in original currency.",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fx_revaluation_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/fx-revaluation returns 200 full page with FX item data."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/fx_revaluation.*$").mock(
        return_value=Response(200, json=_FX_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/fx-revaluation")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Title present
    assert "FX Revaluation" in resp.text
    # Base currency shown
    assert "AUD" in resp.text
    # Invoice item
    assert "INV-000001" in resp.text
    assert "Acme Corp" in resp.text
    assert "USD" in resp.text
    assert "1,000.00" in resp.text
    # Bill item
    assert "BILL-000042" in resp.text
    assert "Overseas Supplier" in resp.text
    assert "EUR" in resp.text
    assert "300.00" in resp.text
    # outstanding_base is null — em-dash placeholder rendered
    assert "&mdash;" in resp.text or "—" in resp.text
    # v1 note about no live rates
    assert "Live FX rates not configured" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_fx_revaluation_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /reports/fx-revaluation with HX-Request returns fragment, no <html>."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/fx_revaluation.*$").mock(
        return_value=Response(200, json=_FX_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/fx-revaluation",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    # Fragment wrapper present
    assert "report-content" in resp.text
    # Data still present
    assert "INV-000001" in resp.text
    assert "Acme Corp" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_fx_revaluation_as_of_date_param(respx_mock: respx.MockRouter) -> None:
    """GET /reports/fx-revaluation?as_of_date=2026-01-01 accepted, no error."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/fx_revaluation.*$").mock(
        return_value=Response(200, json=_FX_REPORT_EMPTY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/fx-revaluation",
            params={"as_of_date": "2026-01-01"},
        )

    assert resp.status_code == 200
    # No error block
    assert "API error" not in resp.text
    # Verify the as_of_date was forwarded to the API
    called_url = str(respx_mock.calls[0].request.url)
    assert "as_of_date=2026-01-01" in called_url


@pytest.mark.anyio
@respx.mock
async def test_fx_revaluation_empty_items(respx_mock: respx.MockRouter) -> None:
    """GET /reports/fx-revaluation with empty items shows no-data message."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/fx_revaluation.*$").mock(
        return_value=Response(200, json=_FX_REPORT_EMPTY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/fx-revaluation")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Empty state message
    assert "No foreign-currency documents found" in resp.text
