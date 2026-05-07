"""Tests for the account archive action — Lane D cycle 23.

Three tests:
1. test_account_archive_happy_path         — API 204 -> 303 to /accounts with flash
2. test_account_archive_in_use             — API 422 (account has transactions) -> flash back to detail
3. test_account_archive_button_not_shown   — already-archived account has no archive form

The accounts detail template shows Edit + Archive buttons only when account.archived_at is falsy.
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

_ACCOUNT_ID = "cccccccc-2323-2323-2323-cccccccccccc"

_MOCK_ACCOUNT_ACTIVE = {
    "id": _ACCOUNT_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "5-9000",
    "name": "Archivable Expense",
    "account_type": "EXPENSE",
    "parent_id": None,
    "tax_code_default": None,
    "is_header": False,
    "reconcile": False,
    "system_managed": False,
    "bsb": None,
    "bank_account_number": None,
    "bank_account_title": None,
    "apca_user_id": None,
    "bank_abbreviation": None,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_ACCOUNT_ARCHIVED = {
    **_MOCK_ACCOUNT_ACTIVE,
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
# 1. Archive happy path — API 204 -> 303 to /accounts with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /accounts/{id}/archive; API 204 -> 303 to /accounts."""
    respx_mock.delete(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect).
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 200, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/accounts/{_ACCOUNT_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/accounts"


# ---------------------------------------------------------------------------
# 2. Archive 422 — API blocks (account in use) -> flash back to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_archive_in_use(respx_mock: respx.MockRouter) -> None:
    """API 422 (account has transactions) -> 303 redirect back to detail with flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(
            422,
            json={"detail": "Cannot archive account with associated transactions."},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/accounts/{_ACCOUNT_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/accounts/{_ACCOUNT_ID}"


# ---------------------------------------------------------------------------
# 3. Archive button NOT shown when account is already archived
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_archive_button_not_shown(respx_mock: respx.MockRouter) -> None:
    """Detail page for an already-archived account must not show the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/accounts/{_ACCOUNT_ID}")

    assert resp.status_code == 200
    # Archive form must not be shown for an already-archived account.
    assert f"/accounts/{_ACCOUNT_ID}/archive" not in resp.text
    # Edit button also not shown.
    assert f"/accounts/{_ACCOUNT_ID}/edit" not in resp.text
