"""Reports hub degrade layer (M2 enterprise views) — acceptance tests.

Bar: a report whose engine fetch fails at the connection level (or returns
one of the engine's module-unavailable 503 shapes) renders the page shell
with the shared degraded panel inline — never a white-screen 500, and for
HTMX refreshes the degraded panel arrives as the fragment. A plain non-503
API error keeps the existing inline error box (now i18n'd), NOT the
degraded panel.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import httpx
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


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-reports", "locale": "en"}
)
_ADMIN_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-reports", "locale": "en", "user_role": "admin"}
)


def _quiet_side_fetches(respx_mock: respx.MockRouter) -> None:
    """Nav/middleware fetches — keep them quiet."""
    empty = {"items": []}
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=empty)
    )


def _client(cookie: str = _SESSION_COOKIE) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: cookie},
    )


@pytest.mark.anyio
@respx.mock
async def test_report_engine_down_renders_degraded_panel_in_shell(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reports/aged_receivables").mock(
        side_effect=httpx.ConnectError("engine down")
    )

    async with _client() as client:
        resp = await client.get("/reports/aged-receivables")

    assert resp.status_code == 200
    assert "data-degraded-panel" in resp.text
    # Page shell survived — nav + report header still render.
    assert "Aged Receivables" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_report_htmx_refresh_degrades_as_fragment(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reports/aged_receivables").mock(
        side_effect=httpx.ReadTimeout("engine slow")
    )

    async with _client() as client:
        resp = await client.get(
            "/reports/aged-receivables", headers={"HX-Request": "true"}
        )

    assert resp.status_code == 200
    assert "data-degraded-panel" in resp.text
    # Fragment, not a full page swap.
    assert "<html" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_report_layer_a_503_stub_degrades(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reports/aged_payables").mock(
        return_value=Response(
            503, json={"status": "unavailable", "module": "reports"}
        )
    )

    async with _client() as client:
        resp = await client.get("/reports/aged-payables")

    assert resp.status_code == 200
    assert "data-degraded-panel" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_report_plain_api_error_keeps_inline_error_box(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reports/aged_receivables").mock(
        return_value=Response(500, json={"detail": "boom"})
    )

    async with _client() as client:
        resp = await client.get("/reports/aged-receivables")

    assert resp.status_code == 200
    assert "data-degraded-panel" not in resp.text
    assert "could not be loaded" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_report_normal_200_renders_table(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reports/aged_receivables").mock(
        return_value=Response(
            200,
            json={
                "as_of_date": "2026-07-18",
                "buckets": ["Current", "30d"],
                "contacts": [],
                "totals": {},
            },
        )
    )

    async with _client() as client:
        resp = await client.get("/reports/aged-receivables")

    assert resp.status_code == 200
    assert "data-degraded-panel" not in resp.text
    assert "aged-receivables-table" in resp.text


# ---------------------------------------------------------------------------
# Every other wired GET report route — same degrade contract as
# aged_receivables above. (name, page_path, engine_endpoints, session_cookie)
# ---------------------------------------------------------------------------

_REPORT_DEGRADE_CASES: list[tuple[str, str, list[str], str]] = [
    ("aged-payables", "/reports/aged-payables", ["/api/v1/reports/aged_payables"], _SESSION_COOKIE),
    (
        "profit-loss",
        "/reports/profit-loss",
        ["/api/v1/reports/profit_loss", "/api/v1/reports/ytd_turnover"],
        _SESSION_COOKIE,
    ),
    ("balance-sheet", "/reports/balance-sheet", ["/api/v1/reports/balance_sheet"], _SESSION_COOKIE),
    ("trial-balance", "/reports/trial-balance", ["/api/v1/reports/trial_balance"], _SESSION_COOKIE),
    ("cashflow", "/reports/cashflow", ["/api/v1/reports/cashflow"], _SESSION_COOKIE),
    ("bas-summary", "/reports/bas-summary", ["/api/v1/reports/bas_summary"], _SESSION_COOKIE),
    (
        "bas-payg",
        "/reports/bas-payg",
        ["/api/v1/reports/bas_summary", "/api/v1/pay-runs"],
        _SESSION_COOKIE,
    ),
    ("budget-vs-actual", "/reports/budget-vs-actual", ["/api/v1/reports/budget_vs_actual"], _SESSION_COOKIE),
    ("pl-by-segment", "/reports/pl-by-segment", ["/api/v1/reports/pl_by_segment"], _SESSION_COOKIE),
    (
        "revenue-by-customer",
        "/reports/revenue-by-customer",
        ["/api/v1/reports/revenue_by_customer"],
        _SESSION_COOKIE,
    ),
    (
        "depreciation-schedule",
        "/reports/depreciation-schedule",
        ["/api/v1/reports/depreciation_schedule"],
        _SESSION_COOKIE,
    ),
    ("fx-revaluation", "/reports/fx-revaluation", ["/api/v1/reports/fx_revaluation"], _SESSION_COOKIE),
    (
        "statement-pack",
        "/reports/statement-pack",
        [
            "/api/v1/reports/profit_loss",
            "/api/v1/reports/balance_sheet",
            "/api/v1/reports/trial_balance",
        ],
        _SESSION_COOKIE,
    ),
    # Admin-gated — needs user_role=admin in the session (see _require_admin).
    ("close-year", "/reports/close-year", ["/api/v1/accounts"], _ADMIN_SESSION_COOKIE),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name, page_path, engine_endpoints, cookie",
    _REPORT_DEGRADE_CASES,
    ids=[case[0] for case in _REPORT_DEGRADE_CASES],
)
@respx.mock
async def test_each_report_engine_down_degrades(
    respx_mock: respx.MockRouter,
    name: str,
    page_path: str,
    engine_endpoints: list[str],
    cookie: str,
) -> None:
    """Every wired GET report route degrades gracefully when its engine
    endpoint(s) are unreachable — same contract as aged_receivables above.
    """
    _quiet_side_fetches(respx_mock)
    for endpoint in engine_endpoints:
        respx_mock.get(f"{_API_BASE}{endpoint}").mock(
            side_effect=httpx.ConnectError("engine down")
        )

    async with _client(cookie) as client:
        resp = await client.get(page_path)

    assert resp.status_code == 200, f"{name}: {resp.text[:500]}"
    assert "data-degraded-panel" in resp.text, f"{name}: degraded panel not rendered"
