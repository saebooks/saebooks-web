"""Tests for the Budget vs Actual HTML view — Lane D cycle 41.

Tests:
1. test_budget_vs_actual_get_200      — full-page GET 200, lines and variance rendered
2. test_budget_vs_actual_htmx_partial — HX-Request returns fragment (no <html>)
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
# Mock API response fixture
# ---------------------------------------------------------------------------

_BVA_REPORT = {
    "year": 2026,
    "month": None,
    "lines": [
        {
            "account_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "account_code": "6000",
            "account_name": "Salaries",
            "budget": 10000.0,
            "actual": 9500.0,
            "variance": 500.0,
            "variance_pct": 5.0,
        },
        {
            "account_id": "bbbb0002-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "account_code": "6100",
            "account_name": "Office Expenses",
            "budget": 2000.0,
            "actual": 2500.0,
            "variance": -500.0,
            "variance_pct": -25.0,
        },
    ],
    "total_budget": 12000.0,
    "total_actual": 12000.0,
    "total_variance": 0.0,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_vs_actual_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/budget-vs-actual returns 200 full page with lines and variance data."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/budget_vs_actual.*$").mock(
        return_value=Response(200, json=_BVA_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/budget-vs-actual")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Budget vs Actual" in resp.text
    # Account lines present
    assert "Salaries" in resp.text
    assert "Office Expenses" in resp.text
    assert "6000" in resp.text
    assert "10000.00" in resp.text
    assert "9500.00" in resp.text
    # Variance column present (positive and negative)
    assert "500.00" in resp.text
    assert "-500.00" in resp.text
    # Colour-coded variance — green for positive, red for negative
    assert "green" in resp.text
    assert "red" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_budget_vs_actual_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /reports/budget-vs-actual with HX-Request returns fragment, no <html>."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/budget_vs_actual.*$").mock(
        return_value=Response(200, json=_BVA_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/budget-vs-actual",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    # Fragment wrapper present
    assert "report-content" in resp.text
    # Data still present
    assert "Salaries" in resp.text
    assert "10,000.00" in resp.text
