"""Tests for the fixed asset edit form — Lane D cycle 31.

Four tests:
1. test_fixed_asset_edit_form_renders        — GET /fixed-assets/{id}/edit has version + pre-filled values
2. test_fixed_asset_edit_disposed_blocked    — GET /fixed-assets/{id}/edit on disposed asset -> 422 + edit_blocked
3. test_fixed_asset_edit_success_redirects   — POST happy path; API 200 -> 303 to detail with flash
4. test_fixed_asset_edit_conflict_shows_banner — API 409 -> re-render with conflict banner + server version
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

_ASSET_ID = "fa222222-2222-2222-2222-aaaaaaaaaaaa"
_COST_ACCT = "cccc1111-1111-1111-1111-cccccccccccc"
_ACCUM_ACCT = "cccc2222-2222-2222-2222-cccccccccccc"
_DEP_ACCT = "cccc3333-3333-3333-3333-cccccccccccc"

_MOCK_ASSET = {
    "id": _ASSET_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "AST-000003",
    "name": "Forklift",
    "description": "Warehouse forklift",
    "status": "active",
    "depreciation_model_id": "asset_10_year_linear",
    "depreciation_model": None,
    "tax_model_id": None,
    "cost_account_id": _COST_ACCT,
    "accum_dep_account_id": _ACCUM_ACCT,
    "dep_expense_account_id": _DEP_ACCT,
    "purchase_date": "2025-06-01",
    "in_service_date": "2025-06-15",
    "cost": "28000.00",
    "residual_value": "2000.00",
    "last_depreciation_posted_through": None,
    "disposal_date": None,
    "disposal_proceeds": None,
    "disposal_journal_id": None,
    "serial_number": "FK-001",
    "manufacturer": "Toyota",
    "model_number": "8FGU25",
    "location": "Warehouse A",
    "custody_person": "Jane Doe",
    "warranty_end": "2028-06-01",
    "extra": None,
    "version": 2,
    "created_at": "2025-06-01T00:00:00Z",
    "updated_at": "2026-01-10T00:00:00Z",
    "archived_at": None,
}

_MOCK_ASSET_DISPOSED = {
    **_MOCK_ASSET,
    "status": "disposed",
    "disposal_date": "2026-03-01",
    "version": 3,
}

# Server version after a 409 conflict.
_MOCK_ASSET_V3 = {
    **_MOCK_ASSET,
    "name": "Forklift (updated by someone else)",
    "version": 3,
}

_MOCK_ACCOUNTS = {
    "items": [
        {"id": _COST_ACCT, "code": "1500", "name": "Fixed Assets"},
        {"id": _ACCUM_ACCT, "code": "1600", "name": "Accum. Depreciation"},
        {"id": _DEP_ACCT, "code": "6000", "name": "Depreciation Expense"},
    ],
    "total": 3,
}

_MOCK_DEP_MODELS = {
    "items": [
        {
            "id": "asset_no_depreciation",
            "method": "no_depreciation",
            "method_number": 0,
            "method_period": 12,
            "rate_pct": None,
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "asset_10_year_linear",
            "method": "linear",
            "method_number": 120,
            "method_period": 12,
            "rate_pct": None,
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "asset_20_year_linear",
            "method": "linear",
            "method_number": 240,
            "method_period": 12,
            "rate_pct": None,
            "created_at": "2026-01-01T00:00:00Z",
        },
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
# 1. GET /fixed-assets/{id}/edit — form renders with version + pre-filled values
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets/{id}/edit returns the form pre-filled from the API response."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/depreciation_models").mock(
        return_value=Response(200, json=_MOCK_DEP_MODELS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/fixed-assets/{_ASSET_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with correct value.
    assert 'name="version"' in resp.text
    assert 'value="2"' in resp.text
    # Pre-filled name.
    assert "Forklift" in resp.text
    # Depreciation model select present, with asset_10_year_linear pre-selected.
    assert '<select' in resp.text
    assert 'name="depreciation_model_id"' in resp.text
    assert 'value="asset_10_year_linear"' in resp.text
    assert '10-year linear' in resp.text
    # Edit fields present.
    assert 'name="name"' in resp.text


# ---------------------------------------------------------------------------
# 2. GET /fixed-assets/{id}/edit on disposed asset -> 422 + edit_blocked
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_edit_disposed_blocked(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets/{id}/edit for a disposed asset returns 422 + edit_blocked."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET_DISPOSED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/fixed-assets/{_ASSET_ID}/edit")

    assert resp.status_code == 422
    # edit_blocked template content.
    assert "Disposed assets cannot be edited" in resp.text
    # Disposal date shown.
    assert "2026-03-01" in resp.text


# ---------------------------------------------------------------------------
# 3. POST /fixed-assets/{id}/edit happy path -> 303 redirect to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/{id}/edit; API 200 -> 303 redirect to /fixed-assets/{id}."""
    respx_mock.patch(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET_V3)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/fixed-assets/{_ASSET_ID}/edit",
            data={
                "name": "Forklift Updated",
                "depreciation_model_id": "asset_10_year_linear",
                "version": "2",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/fixed-assets/{_ASSET_ID}"


# ---------------------------------------------------------------------------
# 4. POST /fixed-assets/{id}/edit — API 409 -> re-render with conflict banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """API 409 -> re-render edit form with conflict banner and server version."""
    respx_mock.patch(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # Re-fetch after conflict.
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET_V3)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/depreciation_models").mock(
        return_value=Response(200, json=_MOCK_DEP_MODELS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/fixed-assets/{_ASSET_ID}/edit",
            data={
                "name": "My Edited Name",
                "version": "1",  # stale
            },
        )

    assert resp.status_code == 409
    # Conflict banner present.
    assert "conflict-banner" in resp.text or "Someone else has updated this asset" in resp.text
    # Server version (3) now in the form.
    assert 'value="3"' in resp.text
    # User's submitted name preserved.
    assert "My Edited Name" in resp.text
