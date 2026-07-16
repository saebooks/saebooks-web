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
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$").mock(
        return_value=Response(200, json={
            "ytd_turnover": 30000.0,
            "threshold": 75000.0,
            "threshold_crossed": False,
            "threshold_approaching": False,
            "fy_start": "2025-07-01",
            "fy_end": "2026-06-30",
        })
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
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": []})
    )
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
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": []})
    )
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
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": []})
    )
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


# ---------------------------------------------------------------------------
# Mock fixtures — cashflow and depreciation
# ---------------------------------------------------------------------------

_CASHFLOW_REPORT = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-24",
    "operating": {
        "net_profit": 9750.0,
        "adjustments": [],
        "total_operating": 9750.0,
    },
    "investing": {
        "asset_purchases": -5000.0,
        "asset_disposals": 0.0,
        "total_investing": -5000.0,
    },
    "financing": {
        "loan_proceeds": 0.0,
        "loan_repayments": -1000.0,
        "total_financing": -1000.0,
    },
    "net_change": 3750.0,
    "opening_cash": 10000.0,
    "closing_cash": 13750.0,
}

_CASHFLOW_NEGATIVE = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-24",
    "operating": {
        "net_profit": -2000.0,
        "adjustments": [],
        "total_operating": -2000.0,
    },
    "investing": {
        "asset_purchases": 0.0,
        "asset_disposals": 0.0,
        "total_investing": 0.0,
    },
    "financing": {
        "loan_proceeds": 0.0,
        "loan_repayments": 0.0,
        "total_financing": 0.0,
    },
    "net_change": -2000.0,
    "opening_cash": 5000.0,
    "closing_cash": 3000.0,
}

_DEPR_REPORT = {
    "as_of_date": "2026-04-24",
    "assets": [
        {
            "asset_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "asset_number": "FA-001",
            "description": "Office Laptop",
            "acquisition_date": "2024-01-15",
            "cost": 2000.0,
            "residual_value": 200.0,
            "useful_life_months": 36,
            "depreciation_method": "linear",
            "accumulated_depreciation": 600.0,
            "current_book_value": 1400.0,
            "next_month_depreciation": 50.0,
            "fully_depreciated": False,
        }
    ],
    "total_cost": 2000.0,
    "total_accumulated": 600.0,
    "total_book_value": 1400.0,
}

_DEPR_REPORT_DV = {
    "as_of_date": "2026-04-24",
    "assets": [
        {
            "asset_id": "bbbb0002-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "asset_number": "FA-002",
            "description": "Delivery Van",
            "acquisition_date": "2023-06-01",
            "cost": 45000.0,
            "residual_value": 5000.0,
            "useful_life_months": 60,
            "depreciation_method": "diminishing_value",
            "accumulated_depreciation": 12000.0,
            "current_book_value": 33000.0,
            "next_month_depreciation": 550.0,
            "fully_depreciated": False,
        }
    ],
    "total_cost": 45000.0,
    "total_accumulated": 12000.0,
    "total_book_value": 33000.0,
}


# ---------------------------------------------------------------------------
# New tests — cycle 29
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cashflow_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/cashflow returns 200 full page with all three sections."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/cashflow.*$").mock(
        return_value=Response(200, json=_CASHFLOW_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/cashflow")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Cash Flow Statement" in resp.text
    assert "Operating Activities" in resp.text
    assert "Investing Activities" in resp.text
    assert "Financing Activities" in resp.text
    assert "9750.00" in resp.text
    assert "13750.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_cashflow_net_change_displayed(respx_mock: respx.MockRouter) -> None:
    """Cashflow net_change rendered green when positive, red when negative."""
    # Positive net change
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/cashflow.*$").mock(
        return_value=Response(200, json=_CASHFLOW_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp_pos = await client.get("/reports/cashflow")

    assert "3750.00" in resp_pos.text
    assert "Net Change in Cash" in resp_pos.text
    # Positive: should have "positive" (green) design-token styling
    assert "var(--pos)" in resp_pos.text

    # Negative net change
    respx_mock.reset()
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/cashflow.*$").mock(
        return_value=Response(200, json=_CASHFLOW_NEGATIVE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp_neg = await client.get("/reports/cashflow")

    assert "-2000.00" in resp_neg.text
    # Negative: should have "negative" (red) design-token styling on the net-change block
    assert "var(--neg)" in resp_neg.text


@pytest.mark.anyio
@respx.mock
async def test_depreciation_schedule_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/depreciation-schedule returns 200 with asset table columns."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/depreciation_schedule.*$").mock(
        return_value=Response(200, json=_DEPR_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/depreciation-schedule")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Depreciation Schedule" in resp.text
    assert "FA-001" in resp.text
    assert "Office Laptop" in resp.text
    assert "linear" in resp.text
    assert "2000.00" in resp.text
    assert "1400.00" in resp.text
    assert "50.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_depreciation_schedule_method_filter(respx_mock: respx.MockRouter) -> None:
    """GET /reports/depreciation-schedule?method=diminishing_value passes method to API."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/depreciation_schedule.*$").mock(
        return_value=Response(200, json=_DEPR_REPORT_DV)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/depreciation-schedule",
            params={"method": "diminishing_value"},
        )

    assert resp.status_code == 200
    assert "FA-002" in resp.text
    assert "Delivery Van" in resp.text
    assert "diminishing_value" in resp.text
    assert "45000.00" in resp.text
    # Verify the API was called with the method param
    called_url = str(respx_mock.calls[0].request.url)
    assert "method=diminishing_value" in called_url
