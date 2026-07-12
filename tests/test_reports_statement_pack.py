"""Web smoke for the financial statement pack (saebooks-web rebuild).

GET /reports/statement-pack bundles P&L + Balance Sheet + Trial Balance into
one printable document with a cover page and trustee declaration, reusing the
existing /api/v1/reports/* endpoints and per-statement table fragments.
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

_PL = {"income": {}, "expenses": {}, "net_profit": 0.0}
_BS: dict = {}
_TB: dict = {}
_COMPANIES = {
    "items": [
        {
            "name": "Sauer Pty Ltd",
            "legal_name": "Sauer Pty Ltd ATF Saueesti Trust",
            "acn": "683 275 756",
            "abn": "",
        }
    ]
}

# ---------------------------------------------------------------------------
# Richer fixtures for comparative tests
# ---------------------------------------------------------------------------

_PL_CURRENT = {
    "income": {
        "INCOME": [
            {
                "account_id": "aa000001-0000-0000-0000-000000000001",
                "account_name": "Consulting Revenue",
                "code": "4000",
                "amount": 12000.0,
            }
        ],
        "OTHER_INCOME": [],
        "total_income": 12000.0,
    },
    "expenses": {
        "EXPENSE": [
            {
                "account_id": "bb000001-0000-0000-0000-000000000002",
                "account_name": "Office Expenses",
                "code": "6000",
                "amount": 3000.0,
            }
        ],
        "COST_OF_SALES": [],
        "OTHER_EXPENSE": [],
        "total_expenses": 3000.0,
    },
    "net_profit": 9000.0,
}

_PL_PRIOR = {
    "income": {
        "INCOME": [
            {
                "account_id": "aa000001-0000-0000-0000-000000000001",
                "account_name": "Consulting Revenue",
                "code": "4000",
                "amount": 8000.0,
            }
        ],
        "OTHER_INCOME": [],
        "total_income": 8000.0,
    },
    "expenses": {
        "EXPENSE": [
            {
                "account_id": "bb000001-0000-0000-0000-000000000002",
                "account_name": "Office Expenses",
                "code": "6000",
                "amount": 2000.0,
            },
            # Account present in prior year but NOT in current year — must appear
            {
                "account_id": "cc000001-0000-0000-0000-000000000003",
                "account_name": "Depreciation",
                "code": "6100",
                "amount": 500.0,
            },
        ],
        "COST_OF_SALES": [],
        "OTHER_EXPENSE": [],
        "total_expenses": 2500.0,
    },
    "net_profit": 5500.0,
}

_BS_CURRENT = {
    "assets": {
        "ASSET": [
            {
                "account_id": "dd000001-0000-0000-0000-000000000004",
                "account_name": "Business Bank Account",
                "code": "1000",
                "balance": 50000.0,
            }
        ],
        "total_assets": 50000.0,
    },
    "liabilities": {
        "LIABILITY": [
            {
                "account_id": "ee000001-0000-0000-0000-000000000005",
                "account_name": "GST Payable",
                "code": "2100",
                "balance": 1000.0,
            }
        ],
        "total_liabilities": 1000.0,
    },
    "equity": {
        "EQUITY": [
            {
                "account_id": "ff000001-0000-0000-0000-000000000006",
                "account_name": "Owner's Equity",
                "code": "3000",
                "balance": 49000.0,
            }
        ],
        "total_equity": 49000.0,
    },
    "balanced": True,
    "difference": 0.0,
}

_BS_PRIOR = {
    "assets": {
        "ASSET": [
            {
                "account_id": "dd000001-0000-0000-0000-000000000004",
                "account_name": "Business Bank Account",
                "code": "1000",
                "balance": 35000.0,
            }
        ],
        "total_assets": 35000.0,
    },
    "liabilities": {
        "LIABILITY": [
            {
                "account_id": "ee000001-0000-0000-0000-000000000005",
                "account_name": "GST Payable",
                "code": "2100",
                "balance": 800.0,
            }
        ],
        "total_liabilities": 800.0,
    },
    "equity": {
        "EQUITY": [
            {
                "account_id": "ff000001-0000-0000-0000-000000000006",
                "account_name": "Owner's Equity",
                "code": "3000",
                "balance": 34200.0,
            }
        ],
        "total_equity": 34200.0,
    },
    "balanced": True,
    "difference": 0.0,
}


# ---------------------------------------------------------------------------
# Original smoke test — must remain green after comparative feature added
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_statement_pack_get_200(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(return_value=Response(200, json=_PL))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies.*$"
    ).mock(return_value=Response(200, json=_COMPANIES))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/statement-pack")

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Special Purpose Financial Statements" in body
    assert "Statement of Profit or Loss" in body
    assert "Statement of Financial Position" in body
    assert "Trial Balance" in body
    assert "Trustee" in body
    assert "Sauer Pty Ltd ATF Saueesti Trust" in body


# ---------------------------------------------------------------------------
# Comparative tests — these FAIL before the feature is implemented
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_statement_pack_comparative_issues_prior_year_requests(
    respx_mock: respx.MockRouter,
) -> None:
    """With comparative=true (default), the route MUST issue prior-year API calls.

    Asserts that profit_loss is called with prior-year from_date/to_date params
    and balance_sheet is called with a prior-year as_of_date param.
    """
    pl_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(return_value=Response(200, json=_PL_CURRENT))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS_CURRENT))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies.*$"
    ).mock(return_value=Response(200, json=_COMPANIES))

    # Use explicit dates to make prior-year assertions deterministic.
    current_from = "2024-07-01"
    current_to = "2025-06-30"
    current_as_of = "2025-06-30"
    expected_prior_from = "2023-07-01"
    expected_prior_to = "2024-06-30"
    expected_prior_as_of = "2024-06-30"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/statement-pack",
            params={
                "from_date": current_from,
                "to_date": current_to,
                "as_of_date": current_as_of,
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text

    # profit_loss must have been called TWICE: once for current, once for prior.
    pl_calls = pl_route.calls
    assert len(pl_calls) == 2, (
        f"Expected 2 profit_loss API calls (current + prior), got {len(pl_calls)}"
    )

    called_urls = [str(call.request.url) for call in pl_calls]

    # One call must have prior from/to params.
    assert any(
        expected_prior_from in url and expected_prior_to in url for url in called_urls
    ), f"No prior-year P&L call found in {called_urls}"

    # The balance_sheet route should also have two calls: current + prior.
    bs_route_calls = [
        call
        for call in respx_mock.calls
        if "/api/v1/reports/balance_sheet" in str(call.request.url)
    ]
    assert len(bs_route_calls) == 2, (
        f"Expected 2 balance_sheet API calls (current + prior), got {len(bs_route_calls)}"
    )
    bs_urls = [str(c.request.url) for c in bs_route_calls]
    assert any(expected_prior_as_of in url for url in bs_urls), (
        f"No prior-year BS call found in {bs_urls}"
    )


@pytest.mark.asyncio
async def test_statement_pack_comparative_renders_prior_column(
    respx_mock: respx.MockRouter,
) -> None:
    """With comparative=true the rendered HTML must contain prior-year amounts.

    Both the current and prior P&L/BS amounts must appear in the output.
    """
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
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(
        side_effect=lambda req: Response(
            200,
            json=_BS_PRIOR if "2024-06-30" in str(req.url) else _BS_CURRENT,
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies.*$"
    ).mock(return_value=Response(200, json=_COMPANIES))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/statement-pack",
            params={
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "as_of_date": "2025-06-30",
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.text

    # Current-year amounts must be visible.
    assert "12,000.00" in body, "Current P&L income (12000) not found"
    assert "9,000.00" in body, "Current P&L net profit (9000) not found"
    assert "50,000.00" in body, "Current BS asset (50000) not found"

    # Prior-year amounts must also be visible.
    assert "8,000.00" in body, "Prior P&L income (8000) not found"
    assert "5,500.00" in body, "Prior P&L net profit (5500) not found"
    assert "35,000.00" in body, "Prior BS asset (35000) not found"

    # Prior-year column header / label must appear.
    assert "Prior" in body or "prior" in body, "No 'Prior' label found in comparative output"


@pytest.mark.asyncio
async def test_statement_pack_comparative_account_mismatch(
    respx_mock: respx.MockRouter,
) -> None:
    """Accounts present only in one period must still appear; the absent column shows blank/0.

    _PL_PRIOR has 'Depreciation' (code 6100) which does not exist in _PL_CURRENT.
    That account must appear in the rendered HTML (prior column with its amount;
    current column blank or 0).
    """
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
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS_CURRENT))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies.*$"
    ).mock(return_value=Response(200, json=_COMPANIES))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/statement-pack",
            params={
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "as_of_date": "2025-06-30",
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.text

    # Depreciation exists in prior only — must appear in the rendered output.
    assert "Depreciation" in body, (
        "'Depreciation' account (prior-only) must appear in comparative pack"
    )
    assert "500.00" in body, "Prior-only Depreciation amount (500) must appear"


@pytest.mark.asyncio
async def test_statement_pack_comparative_false_no_prior_requests(
    respx_mock: respx.MockRouter,
) -> None:
    """With comparative=false the route must NOT issue prior-year API calls."""
    pl_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(return_value=Response(200, json=_PL))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies.*$"
    ).mock(return_value=Response(200, json=_COMPANIES))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/statement-pack",
            params={
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "as_of_date": "2025-06-30",
                "comparative": "false",
            },
        )

    assert resp.status_code == 200, resp.text
    # Only 1 profit_loss call (current only).
    assert len(pl_route.calls) == 1, (
        f"With comparative=false, expected 1 P&L call, got {len(pl_route.calls)}"
    )
    body = resp.text
    # When comparative=false, comp_pl is empty so the pack uses the shared
    # single-column fragment — no <th> "Prior year" cells in the data tables.
    # The filter form label also says "Prior year" but inside <label>, not <th>.
    assert "<th" not in body or "Prior year" not in [
        th.split(">")[1].split("<")[0].strip()
        for th in body.split("<th")
        if ">" in th
    ]
