"""Tests for the account ranges web views — Lane D cycle 46.

Tests:
1.  test_account_ranges_requires_auth         — 303 -> /login without session
2.  test_account_ranges_list_renders          — list page shows range name + prefix mode
3.  test_account_ranges_list_empty            — empty list shows no-ranges message
4.  test_account_range_new_form_renders       — GET /admin/ranges/new has expected fields
5.  test_account_range_create_success         — POST happy path -> 303 to /admin/ranges
6.  test_account_range_create_validation_err  — POST 422 re-renders form with errors
7.  test_account_range_edit_form_renders      — GET /admin/ranges/{id}/edit prefills form
8.  test_account_range_edit_success           — POST edit -> 303 to /admin/ranges
9.  test_account_range_delete_success         — POST /{id}/delete -> 303 to /admin/ranges
10. test_account_ranges_prefix_mode_update    — POST /prefix_mode -> 303 to /admin/ranges
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

_RANGE_ID = "cccccccc-2222-2222-2222-cccccccccccc"

_MOCK_RANGE = {
    "id": _RANGE_ID,
    "name": "Current Assets",
    "prefix": "1",
    "account_type": "ASSET",
    "description": "All current asset accounts",
    "version": 1,
    "created_at": "2026-04-01T00:00:00Z",
}

_MOCK_RANGES_RESPONSE = {
    "items": [_MOCK_RANGE],
    "total": 1,
    "limit": 100,
    "offset": 0,
}

_MOCK_PREFIX_MODE = {"prefix_mode": "first_digit"}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Auth gate — list
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_account_ranges_requires_auth() -> None:
    """GET /admin/ranges without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/ranges")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. List renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_ranges_list_renders(respx_mock: respx.MockRouter) -> None:
    """GET /admin/ranges renders the range name and prefix mode selector."""
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges").mock(
        return_value=Response(200, json=_MOCK_RANGES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges/prefix_mode").mock(
        return_value=Response(200, json=_MOCK_PREFIX_MODE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/ranges")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Current Assets" in resp.text
    # Prefix mode selector present
    assert 'name="prefix_mode"' in resp.text


# ---------------------------------------------------------------------------
# 3. List renders empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_ranges_list_empty(respx_mock: respx.MockRouter) -> None:
    """GET /admin/ranges with no ranges renders the empty state."""
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 100, "offset": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges/prefix_mode").mock(
        return_value=Response(200, json={"prefix_mode": "none"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/ranges")

    assert resp.status_code == 200
    assert "No account ranges" in resp.text


# ---------------------------------------------------------------------------
# 4. New form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_account_range_new_form_renders() -> None:
    """GET /admin/ranges/new renders the create form with expected fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/ranges/new")

    assert resp.status_code == 200
    assert 'name="name"' in resp.text
    assert 'name="prefix"' in resp.text
    assert 'name="account_type"' in resp.text
    assert 'name="description"' in resp.text


# ---------------------------------------------------------------------------
# 5. Create success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_range_create_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ranges/new with valid data -> 303 redirect to /admin/ranges."""
    respx_mock.post(f"{_API_BASE}/api/v1/account_ranges").mock(
        return_value=Response(201, json=_MOCK_RANGE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/ranges/new",
            data={
                "name": "Current Assets",
                "prefix": "1",
                "account_type": "ASSET",
                "description": "All current asset accounts",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ranges"


# ---------------------------------------------------------------------------
# 6. Create validation error re-renders form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_range_create_validation_err(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ranges/new with API 422 re-renders the form with error text."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "name"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/account_ranges").mock(
        return_value=Response(422, json=_422_body)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/ranges/new",
            data={
                "name": "",
                "prefix": "1",
                "account_type": "ASSET",
            },
        )

    assert resp.status_code == 422
    assert 'name="prefix"' in resp.text
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 7. Edit form renders with pre-filled data
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_range_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /admin/ranges/{id}/edit pre-fills the form with existing data."""
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges/{_RANGE_ID}").mock(
        return_value=Response(200, json=_MOCK_RANGE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/admin/ranges/{_RANGE_ID}/edit")

    assert resp.status_code == 200
    assert "Current Assets" in resp.text
    # Version hidden input
    assert 'name="version"' in resp.text


# ---------------------------------------------------------------------------
# 8. Edit success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_range_edit_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ranges/{id}/edit with valid data -> 303 redirect to /admin/ranges."""
    updated = {**_MOCK_RANGE, "name": "Non-Current Assets", "version": 2}
    respx_mock.patch(f"{_API_BASE}/api/v1/account_ranges/{_RANGE_ID}").mock(
        return_value=Response(200, json=updated)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/admin/ranges/{_RANGE_ID}/edit",
            data={
                "name": "Non-Current Assets",
                "prefix": "1",
                "account_type": "ASSET",
                "version": "1",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ranges"


# ---------------------------------------------------------------------------
# 9. Delete success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_range_delete_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ranges/{id}/delete -> 303 redirect to /admin/ranges."""
    respx_mock.delete(f"{_API_BASE}/api/v1/account_ranges/{_RANGE_ID}").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/admin/ranges/{_RANGE_ID}/delete",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ranges"


# ---------------------------------------------------------------------------
# 10. Prefix mode update
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_account_ranges_prefix_mode_update(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ranges/prefix_mode -> 303 redirect to /admin/ranges."""
    respx_mock.patch(f"{_API_BASE}/api/v1/account_ranges/prefix_mode").mock(
        return_value=Response(200, json={"prefix_mode": "first_digit"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/ranges/prefix_mode",
            data={"prefix_mode": "first_digit"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ranges"
