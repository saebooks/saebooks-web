"""Tests for Revenue by Customer HTML view — gap PSI-2.

Tests:
1. test_revenue_by_customer_200           — full-page GET 200 with rows and concentration warning
2. test_revenue_by_customer_htmx_partial  — HX-Request returns fragment (no <html>)
3. test_revenue_by_customer_no_rows       — empty report renders gracefully
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
# Mock API response fixtures
# ---------------------------------------------------------------------------

_CONCENTRATED_REPORT = {
    "from_date": "2025-07-01",
    "to_date": "2026-04-29",
    "rows": [
        {
            "contact_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "contact_name": "Big Corp Pty Ltd",
            "revenue": 90000.0,
            "pct_of_total": 90.0,
        },
        {
            "contact_id": "bbbb0002-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "contact_name": "Small Client",
            "revenue": 10000.0,
            "pct_of_total": 10.0,
        },
    ],
    "total_revenue": 100000.0,
    "top_customer_pct": 90.0,
    "concentration_warning": True,
}

_EMPTY_REPORT = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-30",
    "rows": [],
    "total_revenue": 0.0,
    "top_customer_pct": None,
    "concentration_warning": False,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_revenue_by_customer_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/revenue-by-customer returns 200 full page with rows and warning."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/revenue_by_customer.*$"
    ).mock(return_value=Response(200, json=_CONCENTRATED_REPORT))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/revenue-by-customer")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Customer rows
    assert "Big Corp Pty Ltd" in resp.text
    assert "Small Client" in resp.text
    assert "90000.00" in resp.text
    assert "10000.00" in resp.text
    # Concentration warning rendered
    assert "concentration" in resp.text.lower() or "80/20" in resp.text or "90" in resp.text
    # PSI language
    assert "PSI" in resp.text or "Personal Services" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_revenue_by_customer_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /reports/revenue-by-customer with HX-Request returns fragment, no <html>."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/revenue_by_customer.*$"
    ).mock(return_value=Response(200, json=_CONCENTRATED_REPORT))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/revenue-by-customer",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "report-content" in resp.text
    assert "Big Corp Pty Ltd" in resp.text
    assert "90000.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_revenue_by_customer_no_rows(respx_mock: respx.MockRouter) -> None:
    """GET /reports/revenue-by-customer with empty report renders gracefully."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/revenue_by_customer.*$"
    ).mock(return_value=Response(200, json=_EMPTY_REPORT))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/revenue-by-customer")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # No concentration warning
    assert "concentration_warning" not in resp.text or "concentration risk" not in resp.text
    # Empty-state message
    assert "No invoiced revenue" in resp.text
