"""Tests for the fixed asset dispose action — Lane D cycle 31.

Three tests:
1. test_fixed_asset_dispose_happy_path      — POST /fixed-assets/{id}/dispose; API 200 -> 303 with flash
2. test_fixed_asset_dispose_conflict        — API 409 -> 303 back to detail with conflict flash
3. test_fixed_asset_dispose_button_not_shown — disposed asset detail has no dispose form
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

_ASSET_ID = "fa333333-3333-3333-3333-aaaaaaaaaaaa"
_COST_ACCT = "cccc1111-1111-1111-1111-cccccccccccc"
_ACCUM_ACCT = "cccc2222-2222-2222-2222-cccccccccccc"
_DEP_ACCT = "cccc3333-3333-3333-3333-cccccccccccc"

_MOCK_ASSET_ACTIVE = {
    "id": _ASSET_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "AST-000004",
    "name": "Laser Cutter",
    "description": None,
    "status": "active",
    "depreciation_model_id": "SL-5Y",
    "depreciation_model": None,
    "tax_model_id": None,
    "cost_account_id": _COST_ACCT,
    "accum_dep_account_id": _ACCUM_ACCT,
    "dep_expense_account_id": _DEP_ACCT,
    "purchase_date": "2024-03-01",
    "in_service_date": None,
    "cost": "12000.00",
    "residual_value": "500.00",
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
    "created_at": "2024-03-01T00:00:00Z",
    "updated_at": "2024-03-01T00:00:00Z",
    "archived_at": None,
}

_MOCK_ASSET_DISPOSED = {
    **_MOCK_ASSET_ACTIVE,
    "status": "disposed",
    "disposal_date": "2026-04-01",
    "disposal_proceeds": "1000.00",
    "version": 2,
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Dispose happy path — API 200 -> 303 to detail with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_dispose_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/{id}/dispose; API 200 -> 303 to /fixed-assets/{id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}/dispose").mock(
        return_value=Response(200, json=_MOCK_ASSET_DISPOSED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/fixed-assets/{_ASSET_ID}/dispose",
            data={
                "disposal_date": "2026-04-01",
                "proceeds": "1000.00",
                "version": "1",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/fixed-assets/{_ASSET_ID}"


# ---------------------------------------------------------------------------
# 2. Dispose conflict — API 409 -> 303 back to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_dispose_conflict(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 redirect back to fixed asset detail."""
    respx_mock.post(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}/dispose").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/fixed-assets/{_ASSET_ID}/dispose",
            data={
                "disposal_date": "2026-04-01",
                "proceeds": "1000.00",
                "version": "0",  # stale
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/fixed-assets/{_ASSET_ID}"


# ---------------------------------------------------------------------------
# 3. Dispose button NOT shown when asset is already disposed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fixed_asset_dispose_button_not_shown(respx_mock: respx.MockRouter) -> None:
    """Detail page for a disposed asset must not show the dispose form."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET_DISPOSED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/fixed-assets/{_ASSET_ID}")

    assert resp.status_code == 200
    # Dispose form must not be shown for a disposed asset.
    assert f"/fixed-assets/{_ASSET_ID}/dispose" not in resp.text
    # Edit button also not shown for disposed asset.
    assert f"/fixed-assets/{_ASSET_ID}/edit" not in resp.text
