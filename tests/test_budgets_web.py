"""Tests for the budgets list + detail views — Lane D cycle 27.

Three tests:
1. test_budgets_list_renders      — full-page GET 200 with year/month in body
2. test_budgets_list_htmx_partial — HX-Request returns fragment (no <html>)
3. test_budgets_detail_renders    — detail page shows year, month, amount, account_id
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
# Fixtures / helpers
# ---------------------------------------------------------------------------

_BUDGET_ID = "eeeeeeee-eeee-eeee-eeee-000000000001"
_ACCOUNT_ID = "ffffffff-ffff-ffff-ffff-000000000001"

_MOCK_BUDGET = {
    "id": _BUDGET_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "account_id": _ACCOUNT_ID,
    "year": 2026,
    "month": 6,
    "amount": "15000.00",
    "notes": "Q3 marketing budget",
    "version": 1,
    "created_at": "2026-01-10T08:00:00Z",
    "updated_at": "2026-01-10T08:00:00Z",
    "archived_at": None,
}

_MOCK_BUDGETS_RESPONSE = {
    "items": [_MOCK_BUDGET],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budgets_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /budgets renders year and amount in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/budgets").mock(
        return_value=Response(200, json=_MOCK_BUDGETS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/budgets")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "2026" in resp.text
    # AU pixel-equivalence: pre-8ff3a95 this cell was bare "%.2f" (no
    # thousands separator) — money(..., grouping=False) restores it.
    assert "15000.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_budgets_list_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /budgets with HX-Request header returns fragment, not full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/budgets").mock(
        return_value=Response(200, json=_MOCK_BUDGETS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/budgets",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "2026" in resp.text
    assert "budgets-table" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_budgets_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /budgets/{id} renders year, month name, amount, and account_id."""
    respx_mock.get(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(200, json=_MOCK_BUDGET)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/budgets/{_BUDGET_ID}")

    assert resp.status_code == 200
    assert "2026" in resp.text
    assert "June" in resp.text
    # AU pixel-equivalence — see test_budgets_list_renders comment above.
    assert "15000.00" in resp.text
    assert _ACCOUNT_ID in resp.text
