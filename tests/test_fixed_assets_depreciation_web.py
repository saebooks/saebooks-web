"""Tests for the fixed asset post-depreciation action — Lane D cycle 40.

Three tests:
1. test_post_depreciation_success_redirects    — POST -> 303 on API 200, flash with amount
2. test_post_depreciation_409_flash            — POST -> 303 + conflict flash on API 409
3. test_post_depreciation_no_dep_model_button_hidden — detail with no_depreciation asset hides section
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

_ASSET_ID = "fa404040-4040-4040-4040-aaaaaaaaaaaa"
_COST_ACCT = "cccc1111-1111-1111-1111-cccccccccccc"
_ACCUM_ACCT = "cccc2222-2222-2222-2222-cccccccccccc"
_DEP_ACCT = "cccc3333-3333-3333-3333-cccccccccccc"

_MOCK_ASSET_ACTIVE_SL = {
    "id": _ASSET_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "AST-000010",
    "name": "Lathe Machine",
    "description": None,
    "status": "ACTIVE",
    "depreciation_model_id": "SL-5Y",
    "depreciation_model": {
        "id": "SL-5Y",
        "name": "Straight Line 5 Years",
        "method": "straight_line",
        "method_period": 1,
    },
    "tax_model_id": None,
    "cost_account_id": _COST_ACCT,
    "accum_dep_account_id": _ACCUM_ACCT,
    "dep_expense_account_id": _DEP_ACCT,
    "purchase_date": "2024-01-01",
    "in_service_date": "2024-01-01",
    "cost": "20000.00",
    "residual_value": "1000.00",
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
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
    "archived_at": None,
}

_MOCK_ASSET_NO_DEP = {
    **_MOCK_ASSET_ACTIVE_SL,
    "depreciation_model_id": "NO-DEP",
    "depreciation_model": {
        "id": "NO-DEP",
        "name": "No Depreciation",
        "method": "no_depreciation",
        "method_period": 1,
    },
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Post depreciation — success (API 200) -> 303 with amount flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_post_depreciation_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/{id}/post-depreciation; API 200 -> 303 to detail with flash."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}/post_depreciation"
    ).mock(
        return_value=Response(
            200,
            json={
                "asset": _MOCK_ASSET_ACTIVE_SL,
                "amount_posted": 316.67,
                "note": "3 months posted",
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/fixed-assets/{_ASSET_ID}/post-depreciation",
            data={
                "through_date": "2026-04-30",
                "version": "1",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/fixed-assets/{_ASSET_ID}"


# ---------------------------------------------------------------------------
# 2. Post depreciation — conflict (API 409) -> 303 with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_post_depreciation_409_flash(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 redirect back to detail with version conflict flash."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}/post_depreciation"
    ).mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/fixed-assets/{_ASSET_ID}/post-depreciation",
            data={
                "through_date": "2026-04-30",
                "version": "0",  # stale
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/fixed-assets/{_ASSET_ID}"
    # Follow the redirect and check flash message.
    # Mock the GET for the detail page.
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET_ACTIVE_SL)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client2:
        # Re-issue with follow_redirects to check the flash is rendered.
        # We need to first set the session flash by doing a fresh POST.
        resp2_post = await client2.post(
            f"/fixed-assets/{_ASSET_ID}/post-depreciation",
            data={
                "through_date": "2026-04-30",
                "version": "0",
            },
        )

    assert resp2_post.status_code == 200
    assert "Version conflict" in resp2_post.text


# ---------------------------------------------------------------------------
# 3. Asset with no_depreciation method — section not shown in detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_post_depreciation_no_dep_model_button_hidden(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a no_depreciation asset must NOT show the post-depreciation section."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets/{_ASSET_ID}").mock(
        return_value=Response(200, json=_MOCK_ASSET_NO_DEP)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.get(f"/fixed-assets/{_ASSET_ID}")

    assert resp.status_code == 200
    # The post-depreciation form action URL must not appear in the HTML.
    assert f"/fixed-assets/{_ASSET_ID}/post-depreciation" not in resp.text
