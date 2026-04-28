"""Tests for bank account write paths — Lane D cycle 35.

Seven tests:
 1. test_bank_account_new_form_renders    — GET /bank-accounts/new → 200 with form
 2. test_bank_account_create_success     — POST /bank-accounts/new → 303 on mock 201
 3. test_bank_account_create_422         — POST /bank-accounts/new → 422 re-render on error
 4. test_bank_account_edit_form_renders  — GET /bank-accounts/{id}/edit → 200 pre-filled
 5. test_bank_account_edit_success       — POST /bank-accounts/{id}/edit → 303 on mock 200
 6. test_bank_account_edit_conflict      — POST /bank-accounts/{id}/edit → 409 conflict banner
 7. test_bank_account_archive_success    — POST /bank-accounts/{id}/archive → 303 on mock 204
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

_ACCOUNT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-000000000001"

_MOCK_ACCOUNT = {
    "id": _ACCOUNT_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "BNKANZ001",
    "name": "ANZ Business Cheque",
    "bsb": "012-345",
    "bank_account_number": "123456789",
    "bank_account_title": "SAE Engineering Pty Ltd",
    "apca_user_id": None,
    "bank_abbreviation": "ANZ",
    "is_trust_account": False,
    "version": 1,
    "created_at": "2024-06-01T09:00:00Z",
    "archived_at": None,
}

_TRUST_ACCOUNT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-000000000002"

_MOCK_TRUST_ACCOUNT = {
    **_MOCK_ACCOUNT,
    "id": _TRUST_ACCOUNT_ID,
    "code": "BNKTRST001",
    "name": "CBA Trust Account",
    "bank_abbreviation": "CBA",
    "is_trust_account": True,
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
async def test_bank_account_new_form_renders() -> None:
    """GET /bank-accounts/new returns 200 with the create form."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-accounts/new")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Required form fields must be present.
    assert 'name="code"' in resp.text
    assert 'name="name"' in resp.text
    assert 'name="bsb"' in resp.text
    assert 'name="idempotency_key"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_account_create_success(respx_mock: respx.MockRouter) -> None:
    """POST /bank-accounts/new with valid data → 303 redirect to /bank-accounts/{id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(201, json=_MOCK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bank-accounts/new",
            data={
                "code": "BNKANZ001",
                "name": "ANZ Business Cheque",
                "bsb": "012-345",
                "bank_account_number": "123456789",
                "bank_account_title": "SAE Engineering Pty Ltd",
                "bank_abbreviation": "ANZ",
                "apca_user_id": "",
                "idempotency_key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bank-accounts/{_ACCOUNT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_bank_account_create_422(respx_mock: respx.MockRouter) -> None:
    """POST /bank-accounts/new with invalid data → 422 re-renders the form with errors."""
    respx_mock.post(f"{_API_BASE}/api/v1/bank_accounts").mock(
        return_value=Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "bsb"],
                        "msg": "String should have at least 6 characters",
                        "type": "string_too_short",
                    }
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
            "/bank-accounts/new",
            data={
                "code": "BNKANZ001",
                "name": "ANZ Business Cheque",
                "bsb": "bad",
                "idempotency_key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )

    assert resp.status_code == 422
    assert "String should have at least 6 characters" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_account_edit_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /bank-accounts/{id}/edit returns 200 with the pre-filled edit form."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bank-accounts/{_ACCOUNT_ID}/edit")

    assert resp.status_code == 200
    assert "<html" in resp.text
    # Pre-filled values must appear.
    assert "BNKANZ001" in resp.text
    assert "ANZ Business Cheque" in resp.text
    # Version hidden input must be present.
    assert 'name="version"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_account_edit_success(respx_mock: respx.MockRouter) -> None:
    """POST /bank-accounts/{id}/edit with valid data → 303 redirect to detail page."""
    respx_mock.patch(f"{_API_BASE}/api/v1/bank_accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(
            200, json={**_MOCK_ACCOUNT, "name": "ANZ Business Cheque Updated", "version": 2}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-accounts/{_ACCOUNT_ID}/edit",
            data={
                "code": "BNKANZ001",
                "name": "ANZ Business Cheque Updated",
                "bsb": "012-345",
                "bank_account_number": "123456789",
                "bank_account_title": "SAE Engineering Pty Ltd",
                "bank_abbreviation": "ANZ",
                "apca_user_id": "",
                "version": "1",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bank-accounts/{_ACCOUNT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_bank_account_edit_conflict(respx_mock: respx.MockRouter) -> None:
    """POST /bank-accounts/{id}/edit → 409 re-renders with conflict banner."""
    updated_account = {**_MOCK_ACCOUNT, "name": "Changed By Someone Else", "version": 2}
    # PATCH returns 409; subsequent GET returns the updated server version.
    respx_mock.patch(f"{_API_BASE}/api/v1/bank_accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(
            409,
            json={
                "detail": "version mismatch",
                "current": updated_account,
            },
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bank_accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(200, json=updated_account)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-accounts/{_ACCOUNT_ID}/edit",
            data={
                "code": "BNKANZ001",
                "name": "My Local Edit",
                "bsb": "012-345",
                "version": "1",
            },
        )

    assert resp.status_code == 409
    # Conflict banner must be present.
    assert "conflict-banner" in resp.text
    assert "Someone else has updated this bank account" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_account_archive_success(respx_mock: respx.MockRouter) -> None:
    """POST /bank-accounts/{id}/archive → 303 redirect to /bank-accounts list."""
    respx_mock.delete(f"{_API_BASE}/api/v1/bank_accounts/{_ACCOUNT_ID}").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-accounts/{_ACCOUNT_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/bank-accounts"


@pytest.mark.anyio
@respx.mock
async def test_bank_account_create_trust(respx_mock: respx.MockRouter) -> None:
    """POST /bank-accounts/new with is_trust_account=on → API receives is_trust_account=True."""
    captured: list[dict] = []

    def _capture(request: respx.models.Request) -> Response:
        import json as _json_inner
        captured.append(_json_inner.loads(request.content))
        return Response(201, json=_MOCK_TRUST_ACCOUNT)

    respx_mock.post(f"{_API_BASE}/api/v1/bank_accounts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bank-accounts/new",
            data={
                "code": "BNKTRST001",
                "name": "CBA Trust Account",
                "bsb": "062-000",
                "bank_abbreviation": "CBA",
                "is_trust_account": "on",
                "idempotency_key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bank-accounts/{_TRUST_ACCOUNT_ID}"
    assert captured[0]["is_trust_account"] is True


@pytest.mark.anyio
@respx.mock
async def test_bank_account_new_form_has_trust_checkbox() -> None:
    """GET /bank-accounts/new — form must include the is_trust_account checkbox."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-accounts/new")

    assert resp.status_code == 200
    assert 'name="is_trust_account"' in resp.text
