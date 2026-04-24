"""Tests for the batch depreciation run page — Lane D cycle 42.

Four tests:
1. test_depreciation_run_form_renders         — GET /fixed-assets/depreciation-run → 200 with form
2. test_depreciation_run_submit_success       — POST with through date; API 200 with results → 200 with summary
3. test_depreciation_run_submit_shows_errors  — POST; API 200 with errors list → error list in response
4. test_depreciation_run_link_on_list         — GET /fixed-assets → 200 with depreciation-run link
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
# Constants / helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})

# Minimal mock API response for a successful batch run
_MOCK_RUN_RESPONSE = {
    "through": "2026-04-30",
    "total_assets": 2,
    "total_amount": "633.34",
    "results": [
        {
            "asset_id": "aaaaaaaa-aaaa-aaaa-aaaa-000000000001",
            "asset_code": "AST-000001",
            "amount_posted": "316.67",
            "note": "3 months posted",
        },
        {
            "asset_id": "bbbbbbbb-bbbb-bbbb-bbbb-000000000002",
            "asset_code": "AST-000002",
            "amount_posted": "316.67",
            "note": "3 months posted",
        },
    ],
    "errors": [],
}

# Mock response where results also include errors for some assets
_MOCK_RUN_RESPONSE_WITH_ERRORS = {
    "through": "2026-04-30",
    "total_assets": 1,
    "total_amount": "316.67",
    "results": [
        {
            "asset_id": "aaaaaaaa-aaaa-aaaa-aaaa-000000000001",
            "asset_code": "AST-000001",
            "amount_posted": "316.67",
            "note": "3 months posted",
        },
    ],
    "errors": [
        "AST-000002: no depreciation model configured",
        "AST-000003: asset is disposed",
    ],
}

# Minimal mock list response
_MOCK_ASSETS_LIST = {
    "items": [],
    "total": 0,
    "page": 1,
    "page_size": 50,
}


# ---------------------------------------------------------------------------
# 1. GET /fixed-assets/depreciation-run — form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_depreciation_run_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets/depreciation-run returns 200 with the date input."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/fixed-assets/depreciation-run")

    assert resp.status_code == 200
    assert 'name="through"' in resp.text
    assert "Batch Depreciation Run" in resp.text
    assert "Run depreciation" in resp.text


# ---------------------------------------------------------------------------
# 2. POST /fixed-assets/depreciation-run — success, renders results
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_depreciation_run_submit_success(respx_mock: respx.MockRouter) -> None:
    """POST /fixed-assets/depreciation-run with API 200 renders results inline."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/fixed_assets/depreciation_run_all"
    ).mock(
        return_value=Response(200, json=_MOCK_RUN_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/fixed-assets/depreciation-run",
            data={"through": "2026-04-30"},
        )

    assert resp.status_code == 200
    assert "assets processed" in resp.text
    assert "633.34" in resp.text
    assert "AST-000001" in resp.text
    assert "AST-000002" in resp.text


# ---------------------------------------------------------------------------
# 3. POST /fixed-assets/depreciation-run — API returns errors list
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_depreciation_run_submit_shows_errors(respx_mock: respx.MockRouter) -> None:
    """POST where API 200 body includes errors list — errors are shown in response."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/fixed_assets/depreciation_run_all"
    ).mock(
        return_value=Response(200, json=_MOCK_RUN_RESPONSE_WITH_ERRORS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/fixed-assets/depreciation-run",
            data={"through": "2026-04-30"},
        )

    assert resp.status_code == 200
    assert "processed" in resp.text
    # Both error strings must appear in the rendered page
    assert "AST-000002: no depreciation model configured" in resp.text
    assert "AST-000003: asset is disposed" in resp.text


# ---------------------------------------------------------------------------
# 4. GET /fixed-assets — list page has batch run link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_depreciation_run_link_on_list(respx_mock: respx.MockRouter) -> None:
    """GET /fixed-assets returns 200 and contains a link to /fixed-assets/depreciation-run."""
    respx_mock.get(f"{_API_BASE}/api/v1/fixed_assets").mock(
        return_value=Response(200, json=_MOCK_ASSETS_LIST)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/fixed-assets")

    assert resp.status_code == 200
    assert "depreciation-run" in resp.text
