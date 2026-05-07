"""Tests for the bank rules web views — Lane D cycle 47.

Tests:
1.  test_bank_rules_requires_auth            — 303 -> /login without session
2.  test_bank_rules_list_renders             — list page shows rule name
3.  test_bank_rules_list_empty               — empty list shows no-rules message
4.  test_bank_rule_new_form_renders          — GET /bank-rules/new has expected fields
5.  test_bank_rule_create_success            — POST happy path -> 303 to /bank-rules
6.  test_bank_rule_create_validation_err     — POST 422 re-renders form with errors
7.  test_bank_rule_edit_form_renders         — GET /bank-rules/{id}/edit prefills form
8.  test_bank_rule_edit_success              — POST edit -> 303 to /bank-rules
9.  test_bank_rule_edit_conflict             — POST edit 409 -> conflict banner
10. test_bank_rule_delete_success            — POST /{id}/delete -> 303 to /bank-rules
11. test_bank_rules_apply_all                — POST /apply-all -> redirect with flash
12. test_bank_rules_write_requires_auth      — POST /bank-rules/new without session -> 303
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

_RULE_ID = "aaaabbbb-1111-2222-3333-ccccddddeeee"
_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"

_MOCK_RULE = {
    "id": _RULE_ID,
    "name": "Officeworks stationery",
    "match_field": "narration",
    "match_operator": "contains",
    "match_value": "OFFICEWORKS",
    "action_account_id": _ACCOUNT_ID,
    "action_tax_code_id": None,
    "priority": 10,
    "version": 1,
    "created_at": "2026-04-01T00:00:00Z",
}

_MOCK_RULES_RESPONSE = {
    "items": [_MOCK_RULE],
    "total": 1,
    "limit": 100,
    "offset": 0,
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
async def test_bank_rules_requires_auth() -> None:
    """GET /bank-rules without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/bank-rules")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. List renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rules_list_renders(respx_mock: respx.MockRouter) -> None:
    """GET /bank-rules renders the rule name in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_rules").mock(
        return_value=Response(200, json=_MOCK_RULES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-rules")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Officeworks stationery" in resp.text
    assert "Apply All Rules" in resp.text


# ---------------------------------------------------------------------------
# 3. List renders empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rules_list_empty(respx_mock: respx.MockRouter) -> None:
    """GET /bank-rules with no rules renders the empty state."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_rules").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 100, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-rules")

    assert resp.status_code == 200
    assert "No bank rules" in resp.text


# ---------------------------------------------------------------------------
# 4. New form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bank_rule_new_form_renders() -> None:
    """GET /bank-rules/new renders the create form with expected fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-rules/new")

    assert resp.status_code == 200
    assert 'name="name"' in resp.text
    assert 'name="match_field"' in resp.text
    assert 'name="match_operator"' in resp.text
    assert 'name="match_value"' in resp.text
    assert 'name="action_account_id"' in resp.text
    assert 'name="action_tax_code_id"' in resp.text
    assert 'name="priority"' in resp.text


# ---------------------------------------------------------------------------
# 5. Create success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rule_create_success(respx_mock: respx.MockRouter) -> None:
    """POST /bank-rules/new with valid data -> 303 redirect to /bank-rules."""
    respx_mock.post(f"{_API_BASE}/api/v1/bank_rules").mock(
        return_value=Response(201, json=_MOCK_RULE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bank-rules/new",
            data={
                "name": "Officeworks stationery",
                "match_field": "narration",
                "match_operator": "contains",
                "match_value": "OFFICEWORKS",
                "action_account_id": _ACCOUNT_ID,
                "priority": "10",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/bank-rules"


# ---------------------------------------------------------------------------
# 6. Create validation error re-renders form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rule_create_validation_err(respx_mock: respx.MockRouter) -> None:
    """POST /bank-rules/new with API 422 re-renders the form with error text."""
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
    respx_mock.post(f"{_API_BASE}/api/v1/bank_rules").mock(
        return_value=Response(422, json=_422_body)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bank-rules/new",
            data={
                "name": "",
                "match_field": "narration",
                "match_operator": "contains",
                "match_value": "TEST",
                "action_account_id": _ACCOUNT_ID,
            },
        )

    assert resp.status_code == 422
    assert 'name="match_value"' in resp.text
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 7. Edit form renders with pre-filled data
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rule_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /bank-rules/{id}/edit pre-fills the form with existing data."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_rules/{_RULE_ID}").mock(
        return_value=Response(200, json=_MOCK_RULE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bank-rules/{_RULE_ID}/edit")

    assert resp.status_code == 200
    assert "Officeworks stationery" in resp.text
    assert "OFFICEWORKS" in resp.text
    # Version hidden input present
    assert 'name="version"' in resp.text


# ---------------------------------------------------------------------------
# 8. Edit success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rule_edit_success(respx_mock: respx.MockRouter) -> None:
    """POST /bank-rules/{id}/edit with valid data -> 303 redirect to /bank-rules."""
    updated = {**_MOCK_RULE, "name": "Officeworks (updated)", "version": 2}
    respx_mock.patch(f"{_API_BASE}/api/v1/bank_rules/{_RULE_ID}").mock(
        return_value=Response(200, json=updated)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-rules/{_RULE_ID}/edit",
            data={
                "name": "Officeworks (updated)",
                "match_field": "narration",
                "match_operator": "contains",
                "match_value": "OFFICEWORKS",
                "action_account_id": _ACCOUNT_ID,
                "version": "1",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/bank-rules"


# ---------------------------------------------------------------------------
# 9. Edit conflict — 409 shows banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rule_edit_conflict(respx_mock: respx.MockRouter) -> None:
    """POST /bank-rules/{id}/edit with 409 re-renders with conflict banner."""
    respx_mock.patch(f"{_API_BASE}/api/v1/bank_rules/{_RULE_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # The route re-fetches the latest version after a 409
    latest = {**_MOCK_RULE, "version": 2}
    respx_mock.get(f"{_API_BASE}/api/v1/bank_rules/{_RULE_ID}").mock(
        return_value=Response(200, json=latest)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-rules/{_RULE_ID}/edit",
            data={
                "name": "Officeworks stationery",
                "match_field": "narration",
                "match_operator": "contains",
                "match_value": "OFFICEWORKS",
                "action_account_id": _ACCOUNT_ID,
                "version": "1",
            },
        )

    assert resp.status_code == 409
    assert "modified by another user" in resp.text


# ---------------------------------------------------------------------------
# 10. Delete success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rule_delete_success(respx_mock: respx.MockRouter) -> None:
    """POST /bank-rules/{id}/delete -> 303 redirect to /bank-rules."""
    respx_mock.delete(f"{_API_BASE}/api/v1/bank_rules/{_RULE_ID}").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-rules/{_RULE_ID}/delete",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/bank-rules"


# ---------------------------------------------------------------------------
# 11. Apply all -> redirect with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_rules_apply_all(respx_mock: respx.MockRouter) -> None:
    """POST /bank-rules/apply-all -> 303 redirect to /bank-rules."""
    respx_mock.post(f"{_API_BASE}/api/v1/bank_rules/apply").mock(
        return_value=Response(200, json={"applied": 7})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/bank-rules/apply-all")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/bank-rules"


# ---------------------------------------------------------------------------
# 12. Auth gate on a write endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bank_rules_write_requires_auth() -> None:
    """POST /bank-rules/new without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bank-rules/new",
            data={"name": "test"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
