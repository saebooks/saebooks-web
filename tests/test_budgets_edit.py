"""Tests for the budget edit form — Lane D cycle 31.

Four tests:
1. test_budget_edit_form_renders          — GET /budgets/{id}/edit has version + pre-filled values
2. test_budget_edit_archived_blocked      — GET /budgets/{id}/edit on archived budget -> 422 + edit_blocked
3. test_budget_edit_success_redirects     — POST happy path; API 200 -> 303 to detail with flash
4. test_budget_edit_conflict_shows_banner — API 409 -> re-render with conflict banner + server version
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

_BUDGET_ID = "bbbb2222-2222-2222-2222-bbbbbbbbbbbb"
_ACCOUNT_ID = "aaaa1111-1111-1111-1111-aaaaaaaaaaaa"

_MOCK_BUDGET = {
    "id": _BUDGET_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "account_id": _ACCOUNT_ID,
    "year": 2026,
    "month": 8,
    "amount": "18000.00",
    "notes": "August target",
    "version": 2,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T06:00:00Z",
    "archived_at": None,
}

_MOCK_BUDGET_ARCHIVED = {
    **_MOCK_BUDGET,
    "archived_at": "2026-04-24T10:00:00Z",
    "version": 3,
}

# Server version returned after a 409 conflict.
_MOCK_BUDGET_V3 = {
    **_MOCK_BUDGET,
    "amount": "19000.00",
    "version": 3,
}

_MOCK_ACCOUNTS = {
    "items": [
        {"id": _ACCOUNT_ID, "code": "4000", "name": "Sales Revenue"},
    ],
    "total": 1,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /budgets/{id}/edit — form renders with version + pre-filled values
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /budgets/{id}/edit returns the form pre-filled from the API response."""
    respx_mock.get(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(200, json=_MOCK_BUDGET)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/budgets/{_BUDGET_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present.
    assert 'name="version"' in resp.text
    assert 'value="2"' in resp.text
    # Pre-filled year.
    assert "2026" in resp.text
    # Pre-filled amount.
    assert "18000" in resp.text


# ---------------------------------------------------------------------------
# 2. GET /budgets/{id}/edit on archived budget -> 422 + edit_blocked
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_edit_archived_blocked(respx_mock: respx.MockRouter) -> None:
    """GET /budgets/{id}/edit on an archived budget renders edit_blocked.html with 422."""
    respx_mock.get(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(200, json=_MOCK_BUDGET_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/budgets/{_BUDGET_ID}/edit")

    assert resp.status_code == 422
    # edit_blocked template content.
    assert "Archived budgets cannot be edited" in resp.text
    assert "2026-04-24" in resp.text


# ---------------------------------------------------------------------------
# 3. POST /budgets/{id}/edit happy path -> 303 redirect to detail with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /budgets/{id}/edit; API 200 -> 303 redirect to /budgets/{id}."""
    respx_mock.patch(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(200, json=_MOCK_BUDGET_V3)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/budgets/{_BUDGET_ID}/edit",
            data={
                "amount": "19000.00",
                "version": "2",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/budgets/{_BUDGET_ID}"


# ---------------------------------------------------------------------------
# 4. POST /budgets/{id}/edit — API 409 -> re-render with conflict banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_budget_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """API 409 -> re-render edit form with conflict banner and server version."""
    respx_mock.patch(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # Re-fetch after conflict.
    respx_mock.get(f"{_API_BASE}/api/v1/budgets/{_BUDGET_ID}").mock(
        return_value=Response(200, json=_MOCK_BUDGET_V3)
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
            f"/budgets/{_BUDGET_ID}/edit",
            data={
                "amount": "21000.00",
                "version": "1",
            },
        )

    assert resp.status_code == 409
    # Conflict banner present.
    assert "conflict-banner" in resp.text or "Someone else has updated" in resp.text
    # Server version (3) now in the form.
    assert 'value="3"' in resp.text
