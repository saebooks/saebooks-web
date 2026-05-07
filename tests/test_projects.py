"""Tests for the projects views — Lane D cycles 22 + 34.

Thirteen tests:
 1. test_projects_requires_auth          — 303 → /login without session
 2. test_projects_list_renders_table     — full-page render contains project code
 3. test_projects_list_partial_htmx      — HX-Request returns fragment (no <html>)
 4. test_projects_detail_renders         — detail page shows project code + name
 5. test_projects_detail_404_propagates  — upstream 404 → HTTP 404 response
 6. test_projects_list_status_filter     — status filter param is forwarded to API
 7. test_project_new_form_renders        — GET /projects/new → 200 with form
 8. test_project_create_success          — POST /projects/new → 303 on mock 201
 9. test_project_create_422              — POST /projects/new → 422 re-render on error
10. test_project_edit_form_renders       — GET /projects/{id}/edit → 200 with form
11. test_project_edit_success            — POST /projects/{id}/edit → 303 on mock 200
12. test_project_edit_conflict           — POST /projects/{id}/edit → 409 re-render with banner
13. test_project_archive_success         — POST /projects/{id}/archive → 303 on mock 204
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

_MOCK_PROJECT_ARCHIVED = {
    **_MOCK_PROJECT,
    "status": "ARCHIVED",
    "archived_at": "2026-03-01T08:00:00Z",
    "version": 2,
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
# Read-only tests (cycle 22)
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


# ---------------------------------------------------------------------------
# Write path tests (cycle 34)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_project_new_form_renders() -> None:
    """GET /projects/new returns 200 with the create form."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/projects/new")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Form fields must be present.
    assert 'name="code"' in resp.text
    assert 'name="name"' in resp.text
    assert 'name="status"' in resp.text
    assert 'name="idempotency_key"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_project_create_success(respx_mock: respx.MockRouter) -> None:
    """POST /projects/new with valid data → 303 redirect to /projects/{id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/projects").mock(
        return_value=Response(201, json=_MOCK_PROJECT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/projects/new",
            data={
                "code": "PRJ-001",
                "name": "Office Fit-Out",
                "status": "ACTIVE",
                "idempotency_key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/projects/{_PROJECT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_project_create_422(respx_mock: respx.MockRouter) -> None:
    """POST /projects/new with invalid data → 422 re-renders the form with errors."""
    respx_mock.post(f"{_API_BASE}/api/v1/projects").mock(
        return_value=Response(
            422,
            json={
                "detail": [
                    {"loc": ["body", "code"], "msg": "String should have at least 1 character", "type": "string_too_short"}
                ]
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
            "/projects/new",
            data={
                "code": "",
                "name": "Something",
                "status": "ACTIVE",
                "idempotency_key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )

    assert resp.status_code == 422
    assert "String should have at least 1 character" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_project_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /projects/{id}/edit returns 200 with the pre-filled edit form."""
    respx_mock.get(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(200, json=_MOCK_PROJECT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/projects/{_PROJECT_ID}/edit")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Pre-filled values must appear.
    assert "PRJ-001" in resp.text
    assert "Office Fit-Out" in resp.text
    # Version hidden input must be present.
    assert 'name="version"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_project_edit_success(respx_mock: respx.MockRouter) -> None:
    """POST /projects/{id}/edit with valid data → 303 redirect to detail page."""
    respx_mock.patch(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(200, json={**_MOCK_PROJECT, "name": "Office Fit-Out Updated", "version": 2})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/projects/{_PROJECT_ID}/edit",
            data={
                "code": "PRJ-001",
                "name": "Office Fit-Out Updated",
                "status": "ACTIVE",
                "version": "1",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/projects/{_PROJECT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_project_edit_conflict(respx_mock: respx.MockRouter) -> None:
    """POST /projects/{id}/edit → 409 re-renders with conflict banner."""
    updated_project = {**_MOCK_PROJECT, "name": "Changed By Someone Else", "version": 2}
    # PATCH returns 409; subsequent GET returns the updated server version.
    respx_mock.patch(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(
            409,
            json={
                "detail": "version mismatch",
                "current": updated_project,
            },
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(200, json=updated_project)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/projects/{_PROJECT_ID}/edit",
            data={
                "code": "PRJ-001",
                "name": "My Local Edit",
                "status": "ACTIVE",
                "version": "1",
            },
        )

    assert resp.status_code == 409
    # Conflict banner must be present.
    assert "conflict-banner" in resp.text
    assert "Someone else has updated this project" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_project_archive_success(respx_mock: respx.MockRouter) -> None:
    """POST /projects/{id}/archive → 303 redirect to /projects list."""
    respx_mock.delete(f"{_API_BASE}/api/v1/projects/{_PROJECT_ID}").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/projects/{_PROJECT_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"
