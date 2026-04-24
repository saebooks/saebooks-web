"""Tests for the budget create form — Lane D cycle 31.

Four tests:
1. test_budget_new_form_renders          — GET /budgets/new returns form with all fields
2. test_budget_create_success_redirects  — POST happy path -> 303 to /budgets/{id}
3. test_budget_create_validation_error   — POST 422 per-field -> re-render form with errors
4. test_budget_create_duplicate_key      — POST 422 string detail (duplicate) -> __all__ banner
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

_BUDGET_ID = "bbbb1111-1111-1111-1111-bbbbbbbbbbbb"
_ACCOUNT_ID = "aaaa1111-1111-1111-1111-aaaaaaaaaaaa"

_MOCK_BUDGET = {
    "id": _BUDGET_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "account_id": _ACCOUNT_ID,
    "year": 2026,
    "month": 7,
    "amount": "20000.00",
    "notes": "Q3 target",
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_ACCOUNTS = {
    "items": [
        {"id": _ACCOUNT_ID, "code": "4000", "name": "Sales Revenue"},
        {"id": "aaaa2222-2222-2222-2222-aaaaaaaaaaaa", "code": "5000", "name": "COGS"},
    ],
    "total": 2,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /budgets/new — form renders with expected fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /budgets/new returns the form with all expected fields."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/budgets/new")

    assert resp.status_code == 200
    assert 'name="account_id"' in resp.text
    assert 'name="year"' in resp.text
    assert 'name="month"' in resp.text
    assert 'name="amount"' in resp.text
    assert 'name="notes"' in resp.text
    assert 'name="idempotency_key"' in resp.text
    # Month names in dropdown
    assert "January" in resp.text
    assert "December" in resp.text


# ---------------------------------------------------------------------------
# 2. POST /budgets/new happy path -> 303 redirect to /budgets/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /budgets/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/budgets").mock(
        return_value=Response(201, json=_MOCK_BUDGET)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/budgets/new",
            data={
                "account_id": _ACCOUNT_ID,
                "year": "2026",
                "month": "7",
                "amount": "20000.00",
                "idempotency_key": "11111111-1111-1111-1111-111111111111",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/budgets/{_BUDGET_ID}"


# ---------------------------------------------------------------------------
# 3. POST /budgets/new — 422 per-field validation error -> re-render
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /budgets/new where API returns 422 re-renders the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "amount"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/budgets").mock(
        return_value=Response(422, json=_422_body)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/budgets/new",
            data={
                "account_id": _ACCOUNT_ID,
                "year": "2026",
                "month": "7",
                "idempotency_key": "22222222-2222-2222-2222-222222222222",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered, not a blank page.
    assert 'name="amount"' in resp.text
    # Error text visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /budgets/new — 422 string detail (duplicate key) -> __all__ banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_create_duplicate_key(respx_mock: respx.MockRouter) -> None:
    """POST /budgets/new where API returns a plain string 422 detail -> __all__ banner."""
    respx_mock.post(f"{_API_BASE}/api/v1/budgets").mock(
        return_value=Response(422, json={"detail": "Budget already exists for this account/year/month."})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/budgets/new",
            data={
                "account_id": _ACCOUNT_ID,
                "year": "2026",
                "month": "7",
                "amount": "20000.00",
                "idempotency_key": "33333333-3333-3333-3333-333333333333",
            },
        )

    assert resp.status_code == 422
    # Non-field error banner should show the API message.
    assert "Budget already exists" in resp.text
