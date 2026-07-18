"""Group overview (/group) — multi-entity consolidated dashboard (M2).

Contract: gated on the multi_company license flag; one row per company via
per-request X-Company-Id fan-out; one company's engine failure degrades
that ROW only; the companies/license fetch failing degrades the whole
panel; different report currencies subtotal per currency.
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

_C1 = "11111111-1111-1111-1111-111111111111"
_C2 = "22222222-2222-2222-2222-222222222222"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-group", "locale": "en"}
)


def _companies_payload() -> dict:
    return {
        "items": [
            {"id": _C1, "name": "Sauer Pty Ltd", "currency": "AUD"},
            {"id": _C2, "name": "Sauer OÜ", "currency": "EUR"},
        ],
        "total": 2,
    }


def _quiet_side_fetches(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": []})
    )


def _mock_reports_for(respx_mock: respx.MockRouter, company_id: str, *,
                      net: str = "100.00", ar: str = "10.00", ap: str = "5.00",
                      down: bool = False) -> None:
    """Register the three per-company report mocks, matched on the
    X-Company-Id header override."""
    hdr = {"X-Company-Id": company_id}
    if down:
        respx_mock.get(
            f"{_API_BASE}/api/v1/reports/profit_loss", headers=hdr
        ).mock(side_effect=httpx.ConnectError("company engine down"))
        respx_mock.get(
            f"{_API_BASE}/api/v1/reports/aged_receivables", headers=hdr
        ).mock(side_effect=httpx.ConnectError("company engine down"))
        respx_mock.get(
            f"{_API_BASE}/api/v1/reports/aged_payables", headers=hdr
        ).mock(side_effect=httpx.ConnectError("company engine down"))
        return
    respx_mock.get(
        f"{_API_BASE}/api/v1/reports/profit_loss", headers=hdr
    ).mock(
        return_value=Response(
            200,
            json={
                "income": {"INCOME": [{"account_name": "Sales", "amount": "200.00"}]},
                "expenses": {},
                "net_profit": net,
            },
        )
    )
    respx_mock.get(
        f"{_API_BASE}/api/v1/reports/aged_receivables", headers=hdr
    ).mock(
        return_value=Response(200, json={"buckets": [], "contacts": [], "totals": {"total": ar}})
    )
    respx_mock.get(
        f"{_API_BASE}/api/v1/reports/aged_payables", headers=hdr
    ).mock(
        return_value=Response(200, json={"buckets": [], "contacts": [], "totals": {"total": ap}})
    )


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


@pytest.mark.anyio
@respx.mock
async def test_group_overview_renders_per_company_rows(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_companies_payload())
    )
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        return_value=Response(200, json={"flags": {"multi_company": True}})
    )
    _mock_reports_for(respx_mock, _C1)
    _mock_reports_for(respx_mock, _C2)

    async with _client() as client:
        resp = await client.get("/group")

    assert resp.status_code == 200
    assert "group-overview-table" in resp.text
    assert "Sauer Pty Ltd" in resp.text
    assert "Sauer OÜ" in resp.text
    # Two currencies → two per-currency subtotal rows + the FX note.
    assert "Total · AUD" in resp.text
    assert "Total · EUR" in resp.text
    assert "not translated" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_group_overview_one_company_down_degrades_row_only(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_companies_payload())
    )
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        return_value=Response(200, json={"flags": {"multi_company": True}})
    )
    _mock_reports_for(respx_mock, _C1)
    _mock_reports_for(respx_mock, _C2, down=True)

    async with _client() as client:
        resp = await client.get("/group")

    assert resp.status_code == 200
    # Healthy company still shows numbers; the down one shows the row marker.
    assert "Sauer Pty Ltd" in resp.text
    assert "data-row-degraded" in resp.text
    assert "data-degraded-panel" not in resp.text  # NOT a whole-panel degrade


@pytest.mark.anyio
@respx.mock
async def test_group_overview_not_entitled_shows_upsell(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_companies_payload())
    )
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        return_value=Response(200, json={"flags": {"multi_company": False}})
    )

    async with _client() as client:
        resp = await client.get("/group")

    assert resp.status_code == 200
    assert "Business or higher edition" in resp.text
    assert "group-overview-table" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_group_overview_engine_down_degrades_panel(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        side_effect=httpx.ConnectError("engine down")
    )
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        side_effect=httpx.ConnectError("engine down")
    )

    async with _client() as client:
        resp = await client.get("/group")

    assert resp.status_code == 200
    assert "data-degraded-panel" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_group_overview_single_company_hint(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(
            200, json={"items": [{"id": _C1, "name": "Solo Co"}], "total": 1}
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        return_value=Response(200, json={"flags": {"multi_company": True}})
    )
    _mock_reports_for(respx_mock, _C1)

    async with _client() as client:
        resp = await client.get("/group")

    assert resp.status_code == 200
    assert "Only one company in this tenant" in resp.text
