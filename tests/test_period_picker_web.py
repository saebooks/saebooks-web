"""Period picker — route-level tests (session persistence + preset resolution).

Covers the behaviour that the pure-function tests in test_period.py can't:
  1. `?preset=this_fy` on /reports/profit-loss resolves from/to using the
     ACTIVE COMPANY's fin_year_start_month (not a hardcoded 1 July).
  2. The resolved period is persisted to the session and picked up by a
     later request to a DIFFERENT period-aware report page with no params.
  3. An explicit custom from_date/to_date on a later request overrides the
     persisted preset.
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

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-period"})

_EMPTY_PL = {
    "from_date": "", "to_date": "",
    "income": {"INCOME": [], "OTHER_INCOME": [], "total_income": 0},
    "expenses": {"EXPENSE": [], "COST_OF_SALES": [], "OTHER_EXPENSE": [], "total_expenses": 0},
    "net_profit": 0,
}
_EMPTY_CASHFLOW = {
    "operating": {"net_profit": 0, "adjustments": [], "total_operating": 0},
    "investing": {"items": [], "total_investing": 0},
    "financing": {"items": [], "total_financing": 0},
    "net_change": 0, "opening_cash": 0, "closing_cash": 0,
}
_EMPTY_YTD = {"ytd_turnover": 0, "threshold": 75000, "threshold_crossed": False,
              "threshold_approaching": False, "fy_start": "", "fy_end": ""}


def _mock_company(respx_mock: respx.MockRouter, fin_year_start_month: int) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$").mock(
        return_value=Response(
            200,
            json={"items": [{"id": "co-1", "fin_year_start_month": fin_year_start_month}]},
        )
    )


def _mock_common(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss(\?.*)?$").mock(
        return_value=Response(200, json=_EMPTY_PL)
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover(\?.*)?$").mock(
        return_value=Response(200, json=_EMPTY_YTD)
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/cashflow(\?.*)?$").mock(
        return_value=Response(200, json=_EMPTY_CASHFLOW)
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/modules(\?.*)?$").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_codes(\?.*)?$").mock(
        return_value=Response(200, json={"items": []})
    )


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


@pytest.mark.anyio
@respx.mock
async def test_this_fy_preset_uses_company_fin_year_start_month(
    respx_mock: respx.MockRouter,
) -> None:
    """A calendar-year-FY company (fin_year_start_month=1) resolves 'this_fy'
    to 1 January, not the AU-hardcoded 1 July."""
    _mock_company(respx_mock, fin_year_start_month=1)
    _mock_common(respx_mock)

    async with _client() as client:
        resp = await client.get("/reports/profit-loss?preset=this_fy")

    assert resp.status_code == 200
    import datetime as _dt
    expected_from = _dt.date(_dt.date.today().year, 1, 1).isoformat()
    assert f'value="{expected_from}"' in resp.text
    # Active preset chip is marked current.
    assert 'aria-current="true"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_period_persists_across_report_pages(respx_mock: respx.MockRouter) -> None:
    """Selecting a preset on one report page carries into a later visit to
    a DIFFERENT period-aware report page with no explicit params."""
    _mock_company(respx_mock, fin_year_start_month=1)
    _mock_common(respx_mock)

    async with _client() as client:
        first = await client.get("/reports/profit-loss?preset=calendar_ytd")
        assert first.status_code == 200

        second = await client.get("/reports/cashflow")
        assert second.status_code == 200

    import datetime as _dt
    expected_from = _dt.date(_dt.date.today().year, 1, 1).isoformat()
    assert f'value="{expected_from}"' in second.text
    assert 'aria-current="true"' in second.text


@pytest.mark.anyio
@respx.mock
async def test_explicit_custom_range_overrides_persisted_preset(
    respx_mock: respx.MockRouter,
) -> None:
    """An explicit from_date/to_date on a later request wins over whatever
    preset was persisted from an earlier request."""
    _mock_company(respx_mock, fin_year_start_month=7)
    _mock_common(respx_mock)

    async with _client() as client:
        first = await client.get("/reports/profit-loss?preset=this_fy")
        assert first.status_code == 200

        second = await client.get(
            "/reports/cashflow?from_date=2020-01-01&to_date=2020-01-31"
        )
        assert second.status_code == 200

    assert 'value="2020-01-01"' in second.text
    assert 'value="2020-01-31"' in second.text


@pytest.mark.anyio
@respx.mock
async def test_no_params_no_session_keeps_route_default(
    respx_mock: respx.MockRouter,
) -> None:
    """A fresh session with no query params keeps the route's own default
    (month-to-date) — unchanged behaviour for existing callers."""
    _mock_company(respx_mock, fin_year_start_month=7)
    _mock_common(respx_mock)

    async with _client() as client:
        resp = await client.get("/reports/profit-loss")

    assert resp.status_code == 200
    import datetime as _dt
    today = _dt.date.today()
    expected_from = today.replace(day=1).isoformat()
    assert f'value="{expected_from}"' in resp.text
    assert f'value="{today.isoformat()}"' in resp.text
