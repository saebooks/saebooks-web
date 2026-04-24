"""Tests for the fixed asset create form — Lane D cycle 31.

Four tests:
1. test_fixed_asset_new_form_renders         — GET /fixed-assets/new returns form with all fields
2. test_fixed_asset_create_success_redirects — POST happy path -> 303 to /fixed-assets/{id}
3. test_fixed_asset_create_validation_error  — POST 422 per-field -> re-render form with errors
4. test_fixed_asset_create_sends_idempotency_key — POST includes X-Idempotency-Key header
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

_ASSET_ID = "fa111111-1111-1111-1111-aaaaaaaaaaaa"
_COST_ACCT = "cccc1111-1111-1111-1111-cccccccccccc"
_ACCUM_ACCT = "cccc2222-2222-2222-2222-cccccccccccc"
_DEP_ACCT = "cccc3333-3333-3333-3333-cccccccccccc"

_MOCK_ASSET = {
    "id": _ASSET_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "AST-000002",
    "name": "Welding Machine",
    "description": None,
    "status": "ACTIVE",
    "depreciation_model_id": "SL-5Y",
    "depreciation_model": None,
    "tax_model_id": None,
    "cost_account_id": _COST_ACCT,
    "accum_dep_account_id": _ACCUM_ACCT,
    "dep_expense_account_id": _DEP_ACCT,
    "purchase_date": "2026-01-10",
    "in_service_date": None,
    "cost": "4500.00",
    "residual_value": "0.00",
    "last_depreciation_posted_through": None,
    "disposal_date": None,
    "disposal_proceeds": None,
    "disposal_journal_id": None,
    "serial_number": None,
    "manufacturer": None,
    "model_number": None,
    "location": None,
    "custody_person": None,
    "warranty_end": None,
    "extra": None,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_ACCOUNTS = {
    "items": [
        {"id": _COST_ACCT, "code": "1500", "name": "Fixed Assets"},
        {"id": _ACCUM_ACCT, "code": "1600", "name": "Accum. Depreciation"},
        {"id": _DEP_ACCT, "code": "6000", "name": "Depreciation Expense"},
    ],
    "total": 3,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /fixed-assets/new — form renders with expected fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets/new returns the form with all expected fields."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/fixed-assets/new")

    assert resp.status_code == 200
    assert 'name="name"' in resp.text
    assert 'name="depreciation_model_id"' in resp.text
    assert 'name="cost"' in resp.text
    assert 'name="purchase_date"' in resp.text
    assert 'name="cost_account_id"' in resp.text
    assert 'name="accum_dep_account_id"' in resp.text
    assert 'name="dep_expense_account_id"' in resp.text
    assert 'name="idempotency_key"' in resp.text


# ---------------------------------------------------------------------------
# 2. POST /fixed-assets/new happy path -> 303 redirect to /fixed-assets/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/fixed_assets").mock(
        return_value=Response(201, json=_MOCK_ASSET)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/fixed-assets/new",
            data={
                "name": "Welding Machine",
                "depreciation_model_id": "SL-5Y",
                "cost_account_id": _COST_ACCT,
                "accum_dep_account_id": _ACCUM_ACCT,
                "dep_expense_account_id": _DEP_ACCT,
                "purchase_date": "2026-01-10",
                "cost": "4500.00",
                "idempotency_key": "44444444-4444-4444-4444-444444444444",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/fixed-assets/{_ASSET_ID}"


# ---------------------------------------------------------------------------
# 3. POST /fixed-assets/new — 422 per-field validation error -> re-render
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/new where API returns 422 re-renders the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "depreciation_model_id"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/fixed_assets").mock(
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
            "/fixed-assets/new",
            data={
                "name": "Welding Machine",
                "cost_account_id": _COST_ACCT,
                "accum_dep_account_id": _ACCUM_ACCT,
                "dep_expense_account_id": _DEP_ACCT,
                "purchase_date": "2026-01-10",
                "cost": "4500.00",
                "idempotency_key": "55555555-5555-5555-5555-555555555555",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered, not a blank page.
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "Welding Machine" in resp.text
    # Error text visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /fixed-assets/new — X-Idempotency-Key header forwarded to API
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_create_sends_idempotency_key(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/new passes the idempotency_key field as X-Idempotency-Key header."""
    _idem_key = "66666666-6666-6666-6666-666666666666"
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("x-idempotency-key", ""))
        return Response(201, json=_MOCK_ASSET)

    respx_mock.post(f"{_API_BASE}/api/v1/fixed_assets").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/fixed-assets/new",
            data={
                "name": "Welding Machine",
                "depreciation_model_id": "SL-5Y",
                "cost_account_id": _COST_ACCT,
                "accum_dep_account_id": _ACCUM_ACCT,
                "dep_expense_account_id": _DEP_ACCT,
                "purchase_date": "2026-01-10",
                "cost": "4500.00",
                "idempotency_key": _idem_key,
            },
        )

    assert len(captured) == 1, "Expected exactly one upstream POST call"
    assert captured[0] == _idem_key, f"Expected idempotency key {_idem_key!r}, got {captured[0]!r}"
