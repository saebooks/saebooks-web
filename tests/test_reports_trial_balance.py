"""Tests for the Trial Balance HTML view — Lane D cycle 41.

Tests:
1. test_trial_balance_get_200      — full-page GET 200, account data and balanced indicator
2. test_trial_balance_htmx_partial — HX-Request returns fragment (no <html>)
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

_TB_REPORT = {
    "as_of_date": "2026-04-24",
    "accounts": [
        {
            "account_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "code": "1000",
            "name": "Business Bank Account",
            "account_type": "ASSET",
            "debit_total": 15000.0,
            "credit_total": 2000.0,
            "balance": 13000.0,
        },
        {
            "account_id": "bbbb0002-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "code": "4000",
            "name": "Consulting Revenue",
            "account_type": "INCOME",
            "debit_total": 0.0,
            "credit_total": 13000.0,
            "balance": 13000.0,
        },
    ],
    "total_debits": 15000.0,
    "total_credits": 15000.0,
    "balanced": True,
}

_TB_UNBALANCED = {
    "as_of_date": "2026-04-24",
    "accounts": [
        {
            "account_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "code": "1000",
            "name": "Business Bank Account",
            "account_type": "ASSET",
            "debit_total": 15000.0,
            "credit_total": 0.0,
            "balance": 15000.0,
        }
    ],
    "total_debits": 15000.0,
    "total_credits": 0.0,
    "balanced": False,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_trial_balance_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/trial-balance returns 200 full page with account data and balanced indicator."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$").mock(
        return_value=Response(200, json=_TB_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/trial-balance")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Trial Balance" in resp.text
    # Account data present
    assert "Business Bank Account" in resp.text
    assert "Consulting Revenue" in resp.text
    assert "1000" in resp.text
    assert "15000.00" in resp.text
    # Balanced indicator present
    assert "Balanced" in resp.text
    assert "balanced" in resp.text.lower()


@pytest.mark.anyio
@respx.mock
async def test_trial_balance_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /reports/trial-balance with HX-Request returns fragment, no <html>."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$").mock(
        return_value=Response(200, json=_TB_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/trial-balance",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    # Fragment wrapper present
    assert "report-content" in resp.text
    # Data still present
    assert "Business Bank Account" in resp.text
    assert "15000.00" in resp.text
