"""Tests for the account create form — Lane D cycle 23.

Five tests:
1. test_account_new_form_renders          — GET /accounts/new returns form with all fields
2. test_account_create_success_redirects  — POST happy path -> 303 to /accounts/{id}
3. test_account_create_validation_error   — POST 422 -> re-render form with errors
4. test_account_create_duplicate_code     — POST 422 string detail -> __all__ banner
5. test_account_create_account_type_select_rendered — account_type select has all 8 options
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

_ACCOUNT_ID = "aaaaaaaa-2323-2323-2323-aaaaaaaaaaaa"

_MOCK_ACCOUNT = {
    "id": _ACCOUNT_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "4-1000",
    "name": "Sales Revenue",
    "account_type": "INCOME",
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


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /accounts/new — form renders with expected fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_account_new_form_renders() -> None:
    """GET /accounts/new returns the form with all expected fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts/new")

    assert resp.status_code == 200
    assert 'name="code"' in resp.text
    assert 'name="name"' in resp.text
    assert 'name="account_type"' in resp.text
    assert 'name="parent_id"' in resp.text
    assert 'name="description"' in resp.text
    assert 'name="idempotency_key"' in resp.text


# ---------------------------------------------------------------------------
# 2. POST /accounts/new happy path -> 303 redirect to /accounts/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /accounts/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(201, json=_MOCK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/accounts/new",
            data={
                "code": "4-1000",
                "name": "Sales Revenue",
                "account_type": "INCOME",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/accounts/{_ACCOUNT_ID}"


# ---------------------------------------------------------------------------
# 3. POST /accounts/new — 422 per-field validation error -> re-render
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /accounts/new where API returns 422 re-renders the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "code"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(422, json=_422_body)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/accounts/new",
            data={
                "name": "No Code Account",
                "account_type": "INCOME",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered, not a blank page.
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "No Code Account" in resp.text
    # Error text visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /accounts/new — 422 with string detail (duplicate code) -> __all__ banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_create_duplicate_code(respx_mock: respx.MockRouter) -> None:
    """POST /accounts/new where API returns a plain string 422 detail -> __all__ banner."""
    respx_mock.post(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(422, json={"detail": "Account code already exists for this company."})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/accounts/new",
            data={
                "code": "4-1000",
                "name": "Duplicate Revenue",
                "account_type": "INCOME",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        )

    assert resp.status_code == 422
    # Non-field error banner should show the API message.
    assert "Account code already exists" in resp.text
    # Submitted code preserved.
    assert "4-1000" in resp.text


# ---------------------------------------------------------------------------
# 5. GET /accounts/new — account_type select rendered with all 8 values
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_account_create_account_type_select_rendered() -> None:
    """GET /accounts/new renders account_type select with all 8 AccountType enum values."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts/new")

    assert resp.status_code == 200
    for value in ("ASSET", "LIABILITY", "EQUITY", "INCOME", "OTHER_INCOME", "EXPENSE", "COST_OF_SALES", "OTHER_EXPENSE"):
        assert f'value="{value}"' in resp.text, f"Missing account_type option: {value}"
