"""Tests for the period-locks settings page (list, add, remove).

Nine tests:
1. test_period_locks_requires_auth          — 303 -> /login without session
2. test_period_locks_forbidden_for_non_admin — bookkeeper role -> 403
3. test_period_locks_list_renders            — locks + effective boundary render
4. test_period_locks_empty_state             — no locks -> open-books empty state
5. test_period_lock_create_success           — POST 201 -> 303 with success flash
6. test_period_lock_create_conflict          — POST 409 (non-advancing) -> engine message flashed
7. test_period_lock_create_missing_date      — no locked_through -> no API call, flash
8. test_period_lock_remove_success           — DELETE 204 -> 303 with flash
9. test_period_lock_remove_requires_reason   — blank reason -> no API call, flash
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

_LOCK_ID = "11111111-1111-1111-1111-111111111111"
_OLD_LOCK_ID = "22222222-2222-2222-2222-222222222222"

_MOCK_LOCK = {
    "id": _LOCK_ID,
    "locked_through": "2026-06-30",
    "locked_at": "2026-07-15T00:00:00Z",
    "locked_by": "api:abcd1234…",
    "reason": "FY26 BAS lodged",
}
_MOCK_OLD_LOCK = {
    "id": _OLD_LOCK_ID,
    "locked_through": "2026-03-31",
    "locked_at": "2026-04-29T00:00:00Z",
    "locked_by": "api:abcd1234…",
    "reason": "Q3 FY26 BAS lodged",
}
_MOCK_LOCKS_RESPONSE = {
    "items": [_MOCK_LOCK, _MOCK_OLD_LOCK],
    "effective_locked_through": "2026-06-30",
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_ADMIN_COOKIE = _make_session_cookie(
    {"api_token": "test-token-abc", "user_role": "admin", "is_sae_staff": False}
)
_BOOKKEEPER_COOKIE = _make_session_cookie(
    {"api_token": "test-token-bk", "user_role": "bookkeeper", "is_sae_staff": False}
)


@pytest.mark.anyio
async def test_period_locks_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/settings/period-locks")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
async def test_period_locks_forbidden_for_non_admin() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE},
    ) as client:
        resp = await client.get("/settings/period-locks")

    assert resp.status_code == 403


@pytest.mark.anyio
@respx.mock
async def test_period_locks_list_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(200, json=_MOCK_LOCKS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/settings/period-locks")

    assert resp.status_code == 200
    assert "2026-06-30" in resp.text
    assert "2026-03-31" in resp.text
    assert "FY26 BAS lodged" in resp.text
    # Remove action present per row.
    assert f"/settings/period-locks/{_LOCK_ID}/remove" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_period_locks_empty_state(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(200, json={"items": [], "effective_locked_through": None})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/settings/period-locks")

    assert resp.status_code == 200
    assert "No locks yet" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_period_lock_create_success(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(201, json=_MOCK_LOCK)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/settings/period-locks",
            data={"locked_through": "2026-06-30", "reason": "FY26 BAS lodged"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/period-locks"


@pytest.mark.anyio
@respx.mock
async def test_period_lock_create_conflict(respx_mock: respx.MockRouter) -> None:
    """The engine 409s a lock that doesn't extend the boundary — the page
    flashes the engine's message rather than a generic error."""
    detail = (
        "locked_through 2026-01-31 does not extend beyond the current lock "
        "(2026-06-30); pick a later date"
    )
    respx_mock.post(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(409, json={"detail": detail})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(200, json=_MOCK_LOCKS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            "/settings/period-locks",
            data={"locked_through": "2026-01-31"},
        )

    assert resp.status_code == 200
    assert "does not extend beyond the current lock" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_period_lock_create_missing_date(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(200, json={"items": [], "effective_locked_through": None})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post("/settings/period-locks", data={"reason": "no date"})

    assert resp.status_code == 200
    assert "A lock date is required." in resp.text
    # No POST ever reached the engine.
    assert not any(
        c.request.method == "POST" for c in respx_mock.calls
    )


@pytest.mark.anyio
@respx.mock
async def test_period_lock_remove_success(respx_mock: respx.MockRouter) -> None:
    respx_mock.delete(f"{_API_BASE}/api/v1/period-close/locks/{_LOCK_ID}").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/settings/period-locks/{_LOCK_ID}/remove",
            data={"reason": "Lock added with the wrong date"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/period-locks"
    delete_call = respx_mock.calls.last
    assert "reason=" in str(delete_call.request.url)


@pytest.mark.anyio
@respx.mock
async def test_period_lock_remove_requires_reason(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/period-close/locks").mock(
        return_value=Response(200, json=_MOCK_LOCKS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            f"/settings/period-locks/{_LOCK_ID}/remove", data={"reason": "   "}
        )

    assert resp.status_code == 200
    assert "A reason is required to remove a period lock." in resp.text
    assert not any(
        c.request.method == "DELETE" for c in respx_mock.calls
    )
