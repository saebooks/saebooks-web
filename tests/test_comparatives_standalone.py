"""Tests for prior-year comparatives on standalone P&L, Balance Sheet, and Trial Balance pages.

TDD: written before the feature is implemented. Three route groups:
1. /reports/profit-loss      — comparative=true fetches prior period; false = single column
2. /reports/balance-sheet    — comparative=true fetches prior year-end; false = single column
3. /reports/trial-balance    — comparative=true fetches prior year-end; false = single column

Follows the respx + ASGITransport pattern from test_reports_statement_pack.py.
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


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")

# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

_PL_CURRENT = {
    "from_date": "2024-07-01",
    "to_date": "2025-06-30",
    "income": {
        "INCOME": [
            {
                "account_id": "aa000001-0000-0000-0000-000000000001",
                "account_name": "Consulting Revenue",
                "code": "4000",
                "amount": 15000.0,
            }
        ],
        "OTHER_INCOME": [],
        "total_income": 15000.0,
    },
    "expenses": {
        "EXPENSE": [
            {
                "account_id": "bb000001-0000-0000-0000-000000000002",
                "account_name": "Office Expenses",
                "code": "6000",
                "amount": 4000.0,
            }
        ],
        "COST_OF_SALES": [],
        "OTHER_EXPENSE": [],
        "total_expenses": 4000.0,
    },
    "net_profit": 11000.0,
}

_PL_PRIOR = {
    "from_date": "2023-07-01",
    "to_date": "2024-06-30",
    "income": {
        "INCOME": [
            {
                "account_id": "aa000001-0000-0000-0000-000000000001",
                "account_name": "Consulting Revenue",
                "code": "4000",
                "amount": 9000.0,
            }
        ],
        "OTHER_INCOME": [],
        "total_income": 9000.0,
    },
    "expenses": {
        "EXPENSE": [
            {
                "account_id": "bb000001-0000-0000-0000-000000000002",
                "account_name": "Office Expenses",
                "code": "6000",
                "amount": 2500.0,
            },
            # Prior-only account — must appear when comparative
            {
                "account_id": "cc000001-0000-0000-0000-000000000003",
                "account_name": "Prior Only Expense",
                "code": "6200",
                "amount": 300.0,
            },
        ],
        "COST_OF_SALES": [],
        "OTHER_EXPENSE": [],
        "total_expenses": 2800.0,
    },
    "net_profit": 6200.0,
}

# YTD stub — always needed by profit_loss route
_YTD = {
    "ytd_turnover": 15000.0,
    "threshold": 75000.0,
    "threshold_crossed": False,
    "threshold_approaching": False,
    "fy_start": "2024-07-01",
    "fy_end": "2025-06-30",
}

_BS_CURRENT = {
    "as_of_date": "2025-06-30",
    "assets": {
        "ASSET": [
            {
                "account_id": "dd000001-0000-0000-0000-000000000004",
                "account_name": "Business Bank Account",
                "code": "1000",
                "balance": 60000.0,
            }
        ],
        "total_assets": 60000.0,
    },
    "liabilities": {
        "LIABILITY": [
            {
                "account_id": "ee000001-0000-0000-0000-000000000005",
                "account_name": "GST Payable",
                "code": "2100",
                "balance": 1500.0,
            }
        ],
        "total_liabilities": 1500.0,
    },
    "equity": {
        "EQUITY": [
            {
                "account_id": "ff000001-0000-0000-0000-000000000006",
                "account_name": "Owner's Equity",
                "code": "3000",
                "balance": 58500.0,
            }
        ],
        "total_equity": 58500.0,
    },
    "balanced": True,
    "difference": 0.0,
}

_BS_PRIOR = {
    "as_of_date": "2024-06-30",
    "assets": {
        "ASSET": [
            {
                "account_id": "dd000001-0000-0000-0000-000000000004",
                "account_name": "Business Bank Account",
                "code": "1000",
                "balance": 40000.0,
            }
        ],
        "total_assets": 40000.0,
    },
    "liabilities": {
        "LIABILITY": [
            {
                "account_id": "ee000001-0000-0000-0000-000000000005",
                "account_name": "GST Payable",
                "code": "2100",
                "balance": 900.0,
            }
        ],
        "total_liabilities": 900.0,
    },
    "equity": {
        "EQUITY": [
            {
                "account_id": "ff000001-0000-0000-0000-000000000006",
                "account_name": "Owner's Equity",
                "code": "3000",
                "balance": 39100.0,
            }
        ],
        "total_equity": 39100.0,
    },
    "balanced": True,
    "difference": 0.0,
}

_TB_CURRENT = {
    "as_of_date": "2025-06-30",
    "accounts": [
        {
            "account_id": "aa000001-0000-0000-0000-000000000001",
            "code": "1000",
            "name": "Business Bank Account",
            "account_type": "ASSET",
            "debit_total": 60000.0,
            "credit_total": 5000.0,
            "balance": 55000.0,
        }
    ],
    "total_debits": 60000.0,
    "total_credits": 60000.0,
    "balanced": True,
}

_TB_PRIOR = {
    "as_of_date": "2024-06-30",
    "accounts": [
        {
            "account_id": "aa000001-0000-0000-0000-000000000001",
            "code": "1000",
            "name": "Business Bank Account",
            "account_type": "ASSET",
            "debit_total": 40000.0,
            "credit_total": 3000.0,
            "balance": 37000.0,
        }
    ],
    "total_debits": 40000.0,
    "total_credits": 40000.0,
    "balanced": True,
}

# ===========================================================================
# P&L standalone — comparative tests
# ===========================================================================


@pytest.mark.asyncio
async def test_pl_standalone_comparative_issues_prior_year_request(
    respx_mock: respx.MockRouter,
) -> None:
    """?comparative=true on /reports/profit-loss must issue a prior-year P&L API call."""
    pl_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(return_value=Response(200, json=_PL_CURRENT))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$"
    ).mock(return_value=Response(200, json=_YTD))

    current_from = "2024-07-01"
    current_to = "2025-06-30"
    expected_prior_from = "2023-07-01"
    expected_prior_to = "2024-06-30"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/profit-loss",
            params={
                "from_date": current_from,
                "to_date": current_to,
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text

    # Must issue 2 profit_loss API calls (current + prior)
    assert len(pl_route.calls) == 2, (
        f"Expected 2 profit_loss API calls (current + prior), got {len(pl_route.calls)}"
    )
    called_urls = [str(c.request.url) for c in pl_route.calls]
    assert any(
        expected_prior_from in url and expected_prior_to in url for url in called_urls
    ), f"No prior-year P&L call found in {called_urls}"


@pytest.mark.asyncio
async def test_pl_standalone_comparative_renders_prior_column(
    respx_mock: respx.MockRouter,
) -> None:
    """?comparative=true renders both current and prior amounts in the HTML."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_PL_PRIOR
            if "2023-07-01" in str(req.url) or "2024-06-30" in str(req.url)
            else _PL_CURRENT,
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$"
    ).mock(return_value=Response(200, json=_YTD))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/profit-loss",
            params={
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.text

    # Current-year amounts
    assert "15000.00" in body, "Current income (15000) not found"
    assert "11000.00" in body, "Current net profit (11000) not found"

    # Prior-year amounts
    assert "9000.00" in body, "Prior income (9000) not found"
    assert "6200.00" in body, "Prior net profit (6200) not found"

    # Prior column label
    assert "prior" in body.lower() or "Prior" in body, "No prior-year label in comparative output"


@pytest.mark.asyncio
async def test_pl_standalone_comparative_prior_only_account(
    respx_mock: respx.MockRouter,
) -> None:
    """Accounts present only in prior year must appear (prior_amount set; current 0/blank)."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_PL_PRIOR
            if "2023-07-01" in str(req.url) or "2024-06-30" in str(req.url)
            else _PL_CURRENT,
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$"
    ).mock(return_value=Response(200, json=_YTD))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/profit-loss",
            params={
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Prior Only Expense" in body, "Prior-only account must appear in comparative P&L"
    assert "300.00" in body, "Prior-only account amount (300) must appear"


@pytest.mark.asyncio
async def test_pl_standalone_no_comparative_single_column(
    respx_mock: respx.MockRouter,
) -> None:
    """Without ?comparative=true, no prior-year API call and no prior column in output."""
    pl_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(return_value=Response(200, json=_PL_CURRENT))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$"
    ).mock(return_value=Response(200, json=_YTD))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/profit-loss",
            params={"from_date": "2024-07-01", "to_date": "2025-06-30"},
        )

    assert resp.status_code == 200, resp.text

    # Exactly 1 P&L call — no prior-year fetch
    assert len(pl_route.calls) == 1, (
        f"Without comparative, expected 1 P&L call, got {len(pl_route.calls)}"
    )
    body = resp.text
    # Current amount present
    assert "15000.00" in body

    # Prior amounts must NOT appear
    assert "9000.00" not in body, "Prior income must not appear when comparative=false"
    assert "6200.00" not in body, "Prior net profit must not appear when comparative=false"


@pytest.mark.asyncio
async def test_pl_standalone_htmx_comparative(
    respx_mock: respx.MockRouter,
) -> None:
    """HTMX partial request with ?comparative=true returns fragment (no <html>) with prior column."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_PL_PRIOR
            if "2023-07-01" in str(req.url) or "2024-06-30" in str(req.url)
            else _PL_CURRENT,
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$"
    ).mock(return_value=Response(200, json=_YTD))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/profit-loss",
            params={
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "comparative": "true",
            },
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200, resp.text
    assert "<html" not in resp.text, "HTMX response must be fragment, not full page"
    body = resp.text
    assert "15000.00" in body
    assert "9000.00" in body


# ===========================================================================
# Balance Sheet standalone — comparative tests
# ===========================================================================


@pytest.mark.asyncio
async def test_bs_standalone_comparative_issues_prior_year_request(
    respx_mock: respx.MockRouter,
) -> None:
    """?comparative=true on /reports/balance-sheet must issue a prior-year BS API call."""
    bs_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS_CURRENT))

    current_as_of = "2025-06-30"
    expected_prior_as_of = "2024-06-30"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/balance-sheet",
            params={"as_of_date": current_as_of, "comparative": "true"},
        )

    assert resp.status_code == 200, resp.text

    # Must issue 2 balance_sheet calls
    assert len(bs_route.calls) == 2, (
        f"Expected 2 BS API calls (current + prior), got {len(bs_route.calls)}"
    )
    called_urls = [str(c.request.url) for c in bs_route.calls]
    assert any(expected_prior_as_of in url for url in called_urls), (
        f"No prior-year BS call found in {called_urls}"
    )


@pytest.mark.asyncio
async def test_bs_standalone_comparative_renders_prior_column(
    respx_mock: respx.MockRouter,
) -> None:
    """?comparative=true renders both current and prior balance amounts."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_BS_PRIOR if "2024-06-30" in str(req.url) else _BS_CURRENT,
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/balance-sheet",
            params={"as_of_date": "2025-06-30", "comparative": "true"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.text

    # Current amounts
    assert "60000.00" in body, "Current BS asset (60000) not found"
    assert "58500.00" in body, "Current BS equity (58500) not found"

    # Prior amounts
    assert "40000.00" in body, "Prior BS asset (40000) not found"
    assert "39100.00" in body, "Prior BS equity (39100) not found"

    # Prior label
    assert "prior" in body.lower() or "Prior" in body, "No prior-year label in comparative output"


@pytest.mark.asyncio
async def test_bs_standalone_no_comparative_single_column(
    respx_mock: respx.MockRouter,
) -> None:
    """Without ?comparative=true, no prior-year API call and no prior amounts."""
    bs_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS_CURRENT))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/balance-sheet",
            params={"as_of_date": "2025-06-30"},
        )

    assert resp.status_code == 200, resp.text

    # Exactly 1 BS call
    assert len(bs_route.calls) == 1, (
        f"Without comparative, expected 1 BS call, got {len(bs_route.calls)}"
    )
    body = resp.text
    # Current amount present
    assert "60000.00" in body
    # Prior amounts absent
    assert "40000.00" not in body, "Prior BS asset must not appear when comparative=false"


@pytest.mark.asyncio
async def test_bs_standalone_htmx_comparative(
    respx_mock: respx.MockRouter,
) -> None:
    """HTMX partial with ?comparative=true returns fragment (no <html>) with prior column."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_BS_PRIOR if "2024-06-30" in str(req.url) else _BS_CURRENT,
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/balance-sheet",
            params={"as_of_date": "2025-06-30", "comparative": "true"},
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200, resp.text
    assert "<html" not in resp.text, "HTMX response must be fragment"
    body = resp.text
    assert "60000.00" in body
    assert "40000.00" in body


# ===========================================================================
# Trial Balance standalone — comparative tests
# ===========================================================================


@pytest.mark.asyncio
async def test_tb_standalone_comparative_issues_prior_year_request(
    respx_mock: respx.MockRouter,
) -> None:
    """?comparative=true on /reports/trial-balance must issue a prior-year TB API call."""
    tb_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB_CURRENT))

    current_as_of = "2025-06-30"
    expected_prior_as_of = "2024-06-30"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/trial-balance",
            params={"as_of_date": current_as_of, "comparative": "true"},
        )

    assert resp.status_code == 200, resp.text

    # Must issue 2 trial_balance calls
    assert len(tb_route.calls) == 2, (
        f"Expected 2 TB API calls (current + prior), got {len(tb_route.calls)}"
    )
    called_urls = [str(c.request.url) for c in tb_route.calls]
    assert any(expected_prior_as_of in url for url in called_urls), (
        f"No prior-year TB call found in {called_urls}"
    )


@pytest.mark.asyncio
async def test_tb_standalone_comparative_renders_prior_column(
    respx_mock: respx.MockRouter,
) -> None:
    """?comparative=true renders both current and prior TB balances."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_TB_PRIOR if "2024-06-30" in str(req.url) else _TB_CURRENT,
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/trial-balance",
            params={"as_of_date": "2025-06-30", "comparative": "true"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.text

    # Current amounts
    assert "55000.00" in body, "Current TB balance (55000) not found"
    assert "60000.00" in body, "Current TB debit total (60000) not found"

    # Prior amounts — the comparative column shows prior_balance only
    assert "37000.00" in body, "Prior TB balance (37000) not found"

    # Prior label
    assert "prior" in body.lower() or "Prior" in body, "No prior-year label in comparative output"


@pytest.mark.asyncio
async def test_tb_standalone_no_comparative_single_column(
    respx_mock: respx.MockRouter,
) -> None:
    """Without ?comparative=true, no prior-year API call and no prior amounts."""
    tb_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB_CURRENT))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/trial-balance",
            params={"as_of_date": "2025-06-30"},
        )

    assert resp.status_code == 200, resp.text

    # Exactly 1 TB call
    assert len(tb_route.calls) == 1, (
        f"Without comparative, expected 1 TB call, got {len(tb_route.calls)}"
    )
    body = resp.text
    # Current amount present
    assert "55000.00" in body
    # Prior amounts absent
    assert "37000.00" not in body, "Prior TB balance must not appear when comparative=false"


@pytest.mark.asyncio
async def test_tb_standalone_htmx_comparative(
    respx_mock: respx.MockRouter,
) -> None:
    """HTMX partial with ?comparative=true returns fragment (no <html>) with prior column."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_TB_PRIOR if "2024-06-30" in str(req.url) else _TB_CURRENT,
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/trial-balance",
            params={"as_of_date": "2025-06-30", "comparative": "true"},
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200, resp.text
    assert "<html" not in resp.text, "HTMX response must be fragment"
    body = resp.text
    assert "55000.00" in body
    assert "37000.00" in body
