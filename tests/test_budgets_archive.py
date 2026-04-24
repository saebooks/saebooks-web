"""Tests for the budget archive action — Lane D cycle 31.

Three tests:
1. test_budget_archive_happy_path        — API 204 -> 303 to /budgets with flash
2. test_budget_archive_conflict          — API 409 -> 303 back to detail
3. test_budget_archive_button_not_shown  — archived budget detail has no archive form
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

_BUDGET_ID = "bbbb3333-3333-3333-3333-bbbbbbbbbbbb"
_ACCOUNT_ID = "aaaa1111-1111-1111-1111-aaaaaaaaaaaa"

_MOCK_BUDGET_ACTIVE = {
    "id": _BUDGET_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "account_id": _ACCOUNT_ID,
    "year": 2026,
    "month": 9,
    "amount": "12000.00",
    "notes": None,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_BUDGET_ARCHIVED = {
    **_MOCK_BUDGET_ACTIVE,
    "archived_at": "2026-04-24T10:00:00Z",
    "version": 2,
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Archive happy path — API 204 -> 303 to /budgets with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /budgets/{id}/archive; API 204 -> 303 to /budgets."""
    respx_mock.delete(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect).
    respx_mock.get(f"{_API_BASE}/api/v1/budgets").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/budgets/{_BUDGET_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/budgets"


# ---------------------------------------------------------------------------
# 2. Archive conflict — API 409 -> 303 back to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 redirect back to budget detail."""
    respx_mock.delete(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/budgets/{_BUDGET_ID}/archive",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/budgets/{_BUDGET_ID}"


# ---------------------------------------------------------------------------
# 3. Archive button NOT shown when budget is already archived
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_archive_button_not_shown(respx_mock: respx.MockRouter) -> None:
    """Detail page for an already-archived budget must not show the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(200, json=_MOCK_BUDGET_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/budgets/{_BUDGET_ID}")

    assert resp.status_code == 200
    # Archive form must not be shown for an already-archived budget.
    assert f"/budgets/{_BUDGET_ID}/archive" not in resp.text
    # Edit button also not shown.
    assert f"/budgets/{_BUDGET_ID}/edit" not in resp.text
