"""Tests for the projects list + detail views — Lane D cycle 22.

Six tests:
1. test_projects_requires_auth          — 303 → /login without session
2. test_projects_list_renders_table     — full-page render contains project code
3. test_projects_list_partial_htmx      — HX-Request returns fragment (no <html>)
4. test_projects_detail_renders         — detail page shows project code + name
5. test_projects_detail_404_propagates  — upstream 404 → HTTP 404 response
6. test_projects_list_status_filter     — status filter param is forwarded to API
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

_PROJECT_ID = "11111111-1111-1111-1111-222222222222"

_MOCK_PROJECT = {
    "id": _PROJECT_ID,
    "company_id": "33333333-3333-3333-3333-333333333333",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "PRJ-001",
    "name": "Office Fit-Out",
    "status": "ACTIVE",
    "start_date": "2026-01-01",
    "end_date": "2026-06-30",
    "notes": "Main office renovation.",
    "extra": None,
    "version": 1,
    "created_at": "2026-01-01T08:00:00Z",
    "archived_at": None,
}

_MOCK_PROJECTS_RESPONSE = {
    "items": [_MOCK_PROJECT],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_projects_requires_auth() -> None:
    """GET /projects without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/projects")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_projects_list_renders_table(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /projects renders the project code in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/projects").mock(
        return_value=Response(200, json=_MOCK_PROJECTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/projects")

    assert resp.status_code == 200
    # Full page — must contain the outer HTML scaffold.
    assert "<html" in resp.text
    # Project code should appear.
    assert "PRJ-001" in resp.text
    assert "Office Fit-Out" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_projects_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /projects with HX-Request header returns the fragment, not a full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/projects").mock(
        return_value=Response(200, json=_MOCK_PROJECTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/projects",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    # Fragment must NOT contain the full <html> wrapper.
    assert "<html" not in resp.text
    # But it should still contain the project data.
    assert "PRJ-001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_projects_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /projects/{id} renders the project code + name on the detail page."""
    respx_mock.get(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(200, json=_MOCK_PROJECT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/projects/{_PROJECT_ID}")

    assert resp.status_code == 200
    assert "PRJ-001" in resp.text
    assert "Office Fit-Out" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_projects_detail_404_propagates(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(404, json={"detail": "Project not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/projects/{_PROJECT_ID}")

    assert resp.status_code == 404


@pytest.mark.anyio
@respx.mock
async def test_projects_list_status_filter(respx_mock: respx.MockRouter) -> None:
    """GET /projects?status=ACTIVE forwards the status param to the upstream API."""
    route = respx_mock.get(f"{_API_BASE}/api/v1/projects").mock(
        return_value=Response(200, json=_MOCK_PROJECTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/projects", params={"status": "ACTIVE"})

    assert resp.status_code == 200
    # Verify the status filter was forwarded in the upstream request.
    assert route.called
    called_url = str(route.calls[0].request.url)
    assert "status=ACTIVE" in called_url
