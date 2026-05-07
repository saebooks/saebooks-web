"""Tests for the account edit form — Lane D cycle 23.

Five tests:
1. test_account_edit_form_renders          — GET /accounts/{id}/edit has version + pre-filled values
2. test_account_edit_archived_blocked      — GET /accounts/{id}/edit on archived account -> 422 + edit_blocked
3. test_account_edit_success_redirects     — POST happy path; API 200 -> 303 to detail
4. test_account_edit_conflict_shows_banner — API 409 -> re-render with conflict banner + server version
5. test_account_edit_validation_error      — API 422 -> re-render with field errors, input preserved
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

_ACCOUNT_ID = "bbbbbbbb-2323-2323-2323-bbbbbbbbbbbb"

_MOCK_ACCOUNT = {
    "id": _ACCOUNT_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "1-1100",
    "name": "Cheque Account",
    "account_type": "ASSET",
    "parent_id": None,
    "tax_code_default": None,
    "is_header": False,
    "reconcile": True,
    "system_managed": False,
    "bsb": None,
    "bank_account_number": None,
    "bank_account_title": None,
    "apca_user_id": None,
    "bank_abbreviation": None,
    "version": 3,
    "created_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_ACCOUNT_ARCHIVED = {
    **_MOCK_ACCOUNT,
    "archived_at": "2026-04-24T10:00:00Z",
    "version": 4,
}

# Server version returned after a 409 conflict.
_MOCK_ACCOUNT_V4 = {
    **_MOCK_ACCOUNT,
    "version": 4,
    "name": "Cheque Account (updated by someone else)",
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /accounts/{id}/edit — renders form with version and pre-filled values
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /accounts/{id}/edit renders the form with a hidden version input and pre-filled fields."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/accounts/{_ACCOUNT_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with correct value.
    assert 'name="version"' in resp.text
    assert 'value="3"' in resp.text
    # Fields are pre-filled.
    assert "1-1100" in resp.text
    assert "Cheque Account" in resp.text
    assert 'name="code"' in resp.text
    assert 'name="name"' in resp.text


# ---------------------------------------------------------------------------
# 2. GET /accounts/{id}/edit on archived account -> 422 + edit_blocked template
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_edit_archived_blocked(respx_mock: respx.MockRouter) -> None:
    """GET /accounts/{id}/edit for an archived account returns 422 and the edit_blocked page."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/accounts/{_ACCOUNT_ID}/edit")

    assert resp.status_code == 422
    assert "Archived accounts cannot be edited" in resp.text
    assert "Restore it first" in resp.text


# ---------------------------------------------------------------------------
# 3. POST /accounts/{id}/edit — happy path -> 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /accounts/{id}/edit with correct version; API 200 -> 303 to detail."""
    respx_mock.patch(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/accounts/{_ACCOUNT_ID}/edit",
            data={
                "code": "1-1100",
                "name": "Cheque Account Updated",
                "account_type": "ASSET",
                "version": "3",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/accounts/{_ACCOUNT_ID}"


# ---------------------------------------------------------------------------
# 4. POST /accounts/{id}/edit — 409 conflict -> banner + latest server version
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> re-render form with conflict banner."""
    respx_mock.patch(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # Route re-fetches the account after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT_V4)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/accounts/{_ACCOUNT_ID}/edit",
            data={
                "code": "1-1100",
                "name": "My Edited Account Name",
                "account_type": "ASSET",
                "version": "3",  # stale
            },
        )

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else has updated this account" in resp.text
    # Hidden version updated to server's latest (4).
    assert 'value="4"' in resp.text
    # User's submitted name preserved.
    assert "My Edited Account Name" in resp.text


# ---------------------------------------------------------------------------
# 5. POST /accounts/{id}/edit — 422 validation error -> re-render with errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /accounts/{id}/edit where API returns 422 re-renders with field errors."""
    _422_body = {
        "detail": [
            {
                "type": "string_too_short",
                "loc": ["body", "code"],
                "msg": "String should have at least 1 character",
                "input": "",
            }
        ]
    }
    respx_mock.patch(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(422, json=_422_body)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/accounts/{_ACCOUNT_ID}/edit",
            data={
                "code": "",
                "name": "Account With No Code",
                "account_type": "ASSET",
                "version": "3",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered (not blank).
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "Account With No Code" in resp.text
    # Field error visible.
    assert "String should have at least 1 character" in resp.text
