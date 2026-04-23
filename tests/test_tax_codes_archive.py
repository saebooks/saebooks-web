"""Tests for the tax code archive action — Lane D cycle 24.

Three tests:
1. test_tax_code_archive_happy_path       — API 204 -> 303 to /tax-codes with flash
2. test_tax_code_archive_in_use           — API 422 (tax code in use) -> flash back to detail
3. test_tax_code_archive_button_not_shown — already-archived tax code has no archive form

The tax_codes detail template shows Edit + Archive buttons only when tax_code.archived_at is falsy.
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

_TAX_CODE_ID = "cccccccc-2424-2424-2424-cccccccccccc"

_MOCK_TAX_CODE_ACTIVE = {
    "id": _TAX_CODE_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "CAP",
    "name": "GST on Capital",
    "rate": "10.0",
    "tax_system": "GST",
    "reporting_type": "taxable",
    "description": None,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}

_MOCK_TAX_CODE_ARCHIVED = {
    **_MOCK_TAX_CODE_ACTIVE,
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
# 1. Archive happy path — API 204 -> 303 to /tax-codes with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_code_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /tax-codes/{id}/archive; API 204 -> 303 to /tax-codes."""
    respx_mock.delete(f"{_API_BASE}/api/v1/tax_codes/{_TAX_CODE_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect).
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 200, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/tax-codes/{_TAX_CODE_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/tax-codes"


# ---------------------------------------------------------------------------
# 2. Archive 422 — API blocks (tax code in use) -> flash back to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_code_archive_in_use(respx_mock: respx.MockRouter) -> None:
    """API 422 (tax code in use) -> 303 redirect back to detail with flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/tax_codes/{_TAX_CODE_ID}").mock(
        return_value=Response(
            422,
            json={"detail": "Cannot archive tax code with associated transactions."},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/tax-codes/{_TAX_CODE_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/tax-codes/{_TAX_CODE_ID}"


# ---------------------------------------------------------------------------
# 3. Archive button NOT shown when tax code is already archived
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_code_archive_button_not_shown(respx_mock: respx.MockRouter) -> None:
    """Detail page for an already-archived tax code must not show the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes/{_TAX_CODE_ID}").mock(
        return_value=Response(200, json=_MOCK_TAX_CODE_ARCHIVED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/tax-codes/{_TAX_CODE_ID}")

    assert resp.status_code == 200
    # Archive form must not be shown for an already-archived tax code.
    assert f"/tax-codes/{_TAX_CODE_ID}/archive" not in resp.text
    # Edit button also not shown.
    assert f"/tax-codes/{_TAX_CODE_ID}/edit" not in resp.text
