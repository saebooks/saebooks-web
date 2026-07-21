"""Web tests for the report export menu + generic download proxy.

Covers GET /reports/export/{name}: it forwards the page's query params to the
allowlisted engine endpoint and streams the bytes back with the engine's
Content-Type + Content-Disposition. Also asserts the Export menu renders on a
report page with the right hrefs.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks_web.config import settings
from saebooks_web.main import app


def _make_session_cookie(data: dict) -> str:
    from itsdangerous import TimestampSigner

    signer = TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


def _client(auth: bool = True) -> AsyncClient:
    cookies = {settings.session_cookie_name: _SESSION_COOKIE} if auth else {}
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_export_proxy_csv_streams_bytes_and_headers(respx_mock: respx.MockRouter) -> None:
    body = b"section,code,account_name,amount\nincome,4000,Rev,100.00\n"
    route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss\.csv.*$"
    ).mock(
        return_value=Response(
            200,
            content=body,
            headers={
                "content-type": "text/csv; charset=utf-8",
                "content-disposition": 'attachment; filename="profit_loss_2026-04-01_2026-04-24.csv"',
            },
        )
    )
    async with _client() as client:
        resp = await client.get(
            "/reports/export/profit_loss.csv",
            params={"from_date": "2026-04-01", "to_date": "2026-04-24", "include_draft": "false"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert "profit_loss_2026-04-01_2026-04-24.csv" in resp.headers.get("content-disposition", "")
    assert resp.content == body
    # params forwarded to the engine
    called = str(route.calls[0].request.url)
    assert "from_date=2026-04-01" in called and "to_date=2026-04-24" in called


@pytest.mark.asyncio
async def test_export_proxy_xlsx_content_type(respx_mock: respx.MockRouter) -> None:
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/aged_receivables\.xlsx.*$").mock(
        return_value=Response(200, content=b"PK\x03\x04fake", headers={"content-type": xlsx_mime})
    )
    async with _client() as client:
        resp = await client.get("/reports/export/aged_receivables.xlsx", params={"as_of_date": "2026-04-24"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == xlsx_mime


@pytest.mark.asyncio
async def test_export_proxy_cashbook_summary_maps_to_cashbook_endpoint(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/cashbook/summary\.csv.*$").mock(
        return_value=Response(200, content=b"code,label\n", headers={"content-type": "text/csv; charset=utf-8"})
    )
    async with _client() as client:
        resp = await client.get(
            "/reports/export/cashbook_summary.csv", params={"from": "2026-01-01", "to": "2026-03-31"}
        )
    assert resp.status_code == 200, resp.text
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_export_proxy_unknown_name_404() -> None:
    async with _client() as client:
        resp = await client.get("/reports/export/evil.csv")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_export_proxy_unauthenticated_redirects() -> None:
    async with _client(auth=False) as client:
        resp = await client.get("/reports/export/profit_loss.csv")
    assert resp.status_code == 303
    assert resp.headers.get("location", "").endswith("/login")


@pytest.mark.asyncio
async def test_export_proxy_upstream_error_propagates(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss\.csv.*$").mock(
        return_value=Response(500, json={"detail": "boom"})
    )
    async with _client() as client:
        resp = await client.get("/reports/export/profit_loss.csv", params={"from_date": "x", "to_date": "y"})
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_profit_loss_page_renders_export_menu(respx_mock: respx.MockRouter) -> None:
    pnl = {
        "from_date": "2026-04-01",
        "to_date": "2026-04-24",
        "income": {"INCOME": [], "OTHER_INCOME": [], "total_income": 0.0},
        "expenses": {"EXPENSE": [], "COST_OF_SALES": [], "OTHER_EXPENSE": [], "total_expenses": 0.0},
        "net_profit": 0.0,
    }
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$").mock(
        return_value=Response(200, json=pnl)
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover.*$").mock(
        return_value=Response(200, json={"ytd_turnover": 0.0, "threshold": 75000, "fy_start": "2025-07-01", "fy_end": "2026-06-30"})
    )
    async with _client() as client:
        resp = await client.get("/reports/profit-loss")
    assert resp.status_code == 200, resp.text
    assert "/reports/export/profit_loss.csv?" in resp.text
    assert "/reports/export/profit_loss.xlsx?" in resp.text
    assert "/reports/export/profit_loss.pdf?" in resp.text
