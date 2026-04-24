"""Tests for the journal templates web views — Lane D cycle 46.

Tests:
1.  test_journal_templates_requires_auth         — 303 -> /login without session
2.  test_journal_templates_list_renders          — list page shows template name
3.  test_journal_templates_list_empty            — empty list shows no-templates message
4.  test_journal_template_new_form_renders       — GET /journal-templates/new has name field
5.  test_journal_template_create_success         — POST happy path -> 303 to /journal-templates
6.  test_journal_template_create_validation_err  — POST 422 re-renders form with error
7.  test_journal_template_delete_success         — POST /{id}/delete -> 303 with flash
8.  test_journal_template_apply_redirect         — GET /{id}/apply -> 303 to /journal-entries/new
9.  test_journal_template_new_requires_auth      — GET /new without session -> 303 /login
10. test_journal_template_delete_requires_auth   — POST /delete without session -> 303 /login
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

_TEMPLATE_ID = "bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb"

_MOCK_TEMPLATE = {
    "id": _TEMPLATE_ID,
    "name": "Monthly Depreciation",
    "lines": [
        {
            "account_id": "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa",
            "description": "Depreciation charge",
            "debit": "500.00",
            "credit": "0.00",
        },
        {
            "account_id": "aaaaaaaa-0000-0000-0000-bbbbbbbbbbbb",
            "description": "Accumulated depreciation",
            "debit": "0.00",
            "credit": "500.00",
        },
    ],
    "version": 1,
    "created_at": "2026-04-01T00:00:00Z",
    "archived_at": None,
}

_MOCK_TEMPLATES_RESPONSE = {
    "items": [_MOCK_TEMPLATE],
    "total": 1,
    "limit": 100,
    "offset": 0,
}

_MOCK_APPLY_RESPONSE = {
    "lines": [
        {
            "account_id": "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa",
            "description": "Depreciation charge",
            "debit": "500.00",
            "credit": "0.00",
        },
        {
            "account_id": "aaaaaaaa-0000-0000-0000-bbbbbbbbbbbb",
            "description": "Accumulated depreciation",
            "debit": "0.00",
            "credit": "500.00",
        },
    ]
}


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
async def test_journal_templates_requires_auth() -> None:
    """GET /journal-templates without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/journal-templates")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. List renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_templates_list_renders(respx_mock: respx.MockRouter) -> None:
    """GET /journal-templates renders the template name in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_templates").mock(
        return_value=Response(200, json=_MOCK_TEMPLATES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-templates")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Monthly Depreciation" in resp.text


# ---------------------------------------------------------------------------
# 3. List renders empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_templates_list_empty(respx_mock: respx.MockRouter) -> None:
    """GET /journal-templates with no templates renders the empty state."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_templates").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 100, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-templates")

    assert resp.status_code == 200
    assert "No journal templates" in resp.text


# ---------------------------------------------------------------------------
# 4. New form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_journal_template_new_form_renders() -> None:
    """GET /journal-templates/new renders the create form with expected fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-templates/new")

    assert resp.status_code == 200
    assert 'name="name"' in resp.text
    assert 'name="idempotency_key"' in resp.text
    # Line fields for the starter rows
    assert 'lines[0][account_id]' in resp.text
    assert 'lines[0][debit]' in resp.text
    assert 'lines[0][credit]' in resp.text


# ---------------------------------------------------------------------------
# 5. Create success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_template_create_success(respx_mock: respx.MockRouter) -> None:
    """POST /journal-templates/new with valid data -> 303 redirect to /journal-templates."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_templates").mock(
        return_value=Response(201, json=_MOCK_TEMPLATE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/journal-templates/new",
            data={
                "name": "Monthly Depreciation",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "lines[0][account_id]": "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa",
                "lines[0][debit]": "500",
                "lines[0][credit]": "0",
                "lines[1][account_id]": "aaaaaaaa-0000-0000-0000-bbbbbbbbbbbb",
                "lines[1][debit]": "0",
                "lines[1][credit]": "500",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/journal-templates"


# ---------------------------------------------------------------------------
# 6. Create validation error re-renders form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_template_create_validation_err(respx_mock: respx.MockRouter) -> None:
    """POST /journal-templates/new with API 422 re-renders the form with error text."""
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
    respx_mock.post(f"{_API_BASE}/api/v1/journal_templates").mock(
        return_value=Response(422, json=_422_body)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/journal-templates/new",
            data={
                "name": "",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            },
        )

    assert resp.status_code == 422
    assert 'name="name"' in resp.text
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 7. Delete success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_template_delete_success(respx_mock: respx.MockRouter) -> None:
    """POST /journal-templates/{id}/delete -> 303 redirect to /journal-templates."""
    respx_mock.delete(f"{_API_BASE}/api/v1/journal_templates/{_TEMPLATE_ID}").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-templates/{_TEMPLATE_ID}/delete",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/journal-templates"


# ---------------------------------------------------------------------------
# 8. Apply redirects to journal entries new
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_template_apply_redirect(respx_mock: respx.MockRouter) -> None:
    """GET /journal-templates/{id}/apply -> 303 redirect to /journal-entries/new."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/journal_templates/{_TEMPLATE_ID}/apply"
    ).mock(return_value=Response(200, json=_MOCK_APPLY_RESPONSE))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/journal-templates/{_TEMPLATE_ID}/apply")

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/journal-entries/new")
    assert "from_template=" in loc


# ---------------------------------------------------------------------------
# 9. New form requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_journal_template_new_requires_auth() -> None:
    """GET /journal-templates/new without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/journal-templates/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 10. Delete requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_journal_template_delete_requires_auth() -> None:
    """POST /journal-templates/{id}/delete without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-templates/{_TEMPLATE_ID}/delete",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
