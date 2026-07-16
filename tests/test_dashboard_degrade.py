"""Dashboard per-tile degrade (M2 app-lane step 8) — acceptance test.

The design doc's literal acceptance bar: one failing tile fetch still
yields the other tiles populated + that tile degraded — not a 500.
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
    {"api_token": "test-token-dash", "locale": "en"}
)


def _mock_healthy_except_bills(respx_mock: respx.MockRouter) -> None:
    """200-with-empty-items for every dashboard fetch EXCEPT bills, which
    fails at the connection level."""
    empty = {"items": []}
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bills").mock(
        side_effect=httpx.ConnectError("bills module down")
    )
    respx_mock.get(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reports/ytd_turnover").mock(
        return_value=Response(200, json={"ytd_turnover": "0.00"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reports/revenue_by_customer").mock(
        return_value=Response(200, json={"rows": []})
    )
    # Nav/middleware fetches — keep them quiet.
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=empty)
    )


@pytest.mark.anyio
@respx.mock
async def test_dashboard_one_tile_down_still_renders_others(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_healthy_except_bills(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    # Not a 500, not a full-page degrade — the page rendered.
    assert resp.status_code == 200
    # The AP tile (fed by the failed bills fetches) shows the degraded panel.
    assert "data-degraded-panel" in resp.text
    # The cash-flow tile (payments fetch, healthy) still rendered its markup.
    assert "cashSpark" in resp.text
    # Receivables aging (invoices, healthy) rendered too.
    assert "Receivables aging" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_all_healthy_no_degraded_panel(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_healthy_except_bills(respx_mock)
    # Overwrite bills with a healthy mock for the control case.
    respx_mock.get(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "data-degraded-panel" not in resp.text
