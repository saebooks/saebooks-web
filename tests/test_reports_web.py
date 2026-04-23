"""Tests for the HTML report views — Lane D cycle 28.

Seven tests:
1. test_aged_receivables_get_200          — full-page GET 200, contact + bucket in HTML
2. test_aged_receivables_htmx_partial     — HX-Request returns fragment (no <html>)
3. test_aged_payables_get_200             — full-page GET 200, contact + bucket in HTML
4. test_profit_loss_get_200               — full-page GET 200, account name + net profit
5. test_balance_sheet_get_200             — full-page GET 200, balanced indicator in HTML
6. test_bas_summary_get_200               — full-page GET 200, BAS labels in HTML
7. test_bas_summary_shows_remit_or_refund — REMIT rendered when net_gst > 0
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
# Mock response fixtures
# ---------------------------------------------------------------------------

_AGED_REPORT = {
    "as_of_date": "2026-04-24",
    "buckets": ["current", "1-30 days", "31-60 days", "61-90 days", "90+ days"],
    "contacts": [
        {
            "contact_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "contact_name": "Acme Corp",
            "current": 500.0,
            "1-30 days": 200.0,
            "31-60 days": 0.0,
            "61-90 days": 0.0,
            "90+ days": 0.0,
            "total": 700.0,
        }
    ],
    "totals": {
        "current": 500.0,
        "1-30 days": 200.0,
        "31-60 days": 0.0,
        "61-90 days": 0.0,
        "90+ days": 0.0,
        "total": 700.0,
    },
}

_PNL_REPORT = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-24",
    "income": {
        "INCOME": [
            {
                "account_id": "bbbb0001-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "account_name": "Consulting Revenue",
                "code": "4000",
                "amount": 10000.0,
            }
        ],
        "OTHER_INCOME": [],
        "total_income": 10000.0,
    },
    "expenses": {
        "EXPENSE": [
            {
                "account_id": "cccc0001-cccc-cccc-cccc-cccccccccccc",
                "account_name": "Office Supplies",
                "code": "6000",
                "amount": 250.0,
            }
        ],
        "COST_OF_SALES": [],
        "OTHER_EXPENSE": [],
        "total_expenses": 250.0,
    },
    "net_profit": 9750.0,
}

_BS_REPORT = {
    "as_of_date": "2026-04-24",
    "assets": {
        "ASSET": [
            {
                "account_id": "dddd0001-dddd-dddd-dddd-dddddddddddd",
                "account_name": "Business Bank Account",
                "code": "1000",
                "balance": 15000.0,
            }
        ],
        "total_assets": 15000.0,
    },
    "liabilities": {
        "LIABILITY": [],
        "total_liabilities": 0.0,
    },
    "equity": {
        "EQUITY": [
            {
                "account_id": "eeee0001-eeee-eeee-eeee-eeeeeeeeeeee",
                "account_name": "Owner Equity",
                "code": "3000",
                "balance": 15000.0,
            }
        ],
        "total_equity": 15000.0,
    },
    "balanced": True,
    "difference": 0.0,
}

_BAS_REMIT = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-24",
    "g1_total_sales": 10000.0,
    "g2_export_sales": 0.0,
    "g3_other_gst_free_sales": 0.0,
    "g10_capital_acquisitions": 0.0,
    "g11_other_acquisitions": 2750.0,
    "label_1a_gst_on_sales": 1000.0,
    "label_1b_gst_on_purchases": 250.0,
    "net_gst": 750.0,
    "remit_or_refund": "REMIT",
}

_BAS_REFUND = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-24",
    "g1_total_sales": 0.0,
    "g2_export_sales": 0.0,
    "g3_other_gst_free_sales": 0.0,
    "g10_capital_acquisitions": 0.0,
    "g11_other_acquisitions": 5500.0,
    "label_1a_gst_on_sales": 0.0,
    "label_1b_gst_on_purchases": 500.0,
    "net_gst": -500.0,
    "remit_or_refund": "REFUND",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_aged_receivables_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/aged-receivables returns 200 full page with contact and bucket data."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/aged_receivables.*$").mock(
        return_value=Response(200, json=_AGED_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/aged-receivables")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Aged Receivables" in resp.text
    assert "Acme Corp" in resp.text
    assert "700.00" in resp.text
    # Bucket headers
    assert "1-30 days" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_aged_receivables_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /reports/aged-receivables with HX-Request returns fragment, no <html>."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/aged_receivables.*$").mock(
        return_value=Response(200, json=_AGED_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/aged-receivables",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "Acme Corp" in resp.text
    assert "report-content" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_aged_payables_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/aged-payables returns 200 full page with contact and bucket data."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/aged_payables.*$").mock(
        return_value=Response(200, json=_AGED_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/aged-payables")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Aged Payables" in resp.text
    assert "Acme Corp" in resp.text
    assert "700.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_profit_loss_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/profit-loss returns 200 full page with account lines and net profit."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$").mock(
        return_value=Response(200, json=_PNL_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/profit-loss")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Profit" in resp.text
    assert "Consulting Revenue" in resp.text
    assert "Office Supplies" in resp.text
    assert "9750.00" in resp.text
    # Net profit section (positive → green highlight)
    assert "Net Profit" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_balance_sheet_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/balance-sheet returns 200 full page with balanced indicator."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$").mock(
        return_value=Response(200, json=_BS_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/balance-sheet")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Balance Sheet" in resp.text
    assert "Business Bank Account" in resp.text
    assert "15000.00" in resp.text
    # Balanced indicator: green check
    assert "balanced" in resp.text.lower()


@pytest.mark.anyio
@respx.mock
async def test_bas_summary_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/bas-summary returns 200 full page with all BAS labels."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/bas_summary.*$").mock(
        return_value=Response(200, json=_BAS_REMIT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/bas-summary")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "BAS Summary" in resp.text
    # All required BAS labels present
    assert "G1" in resp.text
    assert "G3" in resp.text
    assert "G11" in resp.text
    assert "1A" in resp.text
    assert "1B" in resp.text
    assert "Net GST" in resp.text
    assert "10000.00" in resp.text
    assert "1000.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bas_summary_shows_remit_or_refund(respx_mock: respx.MockRouter) -> None:
    """BAS summary shows REMIT when net_gst > 0 and REFUND when net_gst < 0."""
    # First: REMIT scenario
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/bas_summary.*$").mock(
        return_value=Response(200, json=_BAS_REMIT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp_remit = await client.get("/reports/bas-summary")

    assert "REMIT" in resp_remit.text
    assert "REFUND" not in resp_remit.text

    # Second: REFUND scenario — clear mocks and re-register
    respx_mock.reset()
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/bas_summary.*$").mock(
        return_value=Response(200, json=_BAS_REFUND)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp_refund = await client.get("/reports/bas-summary")

    assert "REFUND" in resp_refund.text
    assert "REMIT" not in resp_refund.text
