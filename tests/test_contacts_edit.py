"""Tests for the contact edit form — Lane D cycles 8, 43.

Six tests:
1. test_edit_form_renders_with_version   — GET /contacts/{id}/edit has version hidden input + bank fields
2. test_edit_success_redirects           — POST with correct version, API 200 → 303
3. test_edit_sends_if_match_header       — PATCH call includes If-Match: <version>
4. test_edit_conflict_shows_banner       — API 409 → re-render with conflict banner + latest version
5. test_edit_validation_error            — API 422 → re-render with field errors
6. test_edit_not_found                   — API 404 on GET → HTTP 404
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

_CONTACT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ACCOUNT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "name": "Revenue", "code": "4000", "account_type": "INCOME"}
_MOCK_ACCOUNTS = {"items": [_MOCK_ACCOUNT], "total": 1, "limit": 1000, "offset": 0}

_TAX_CODE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "code": "GST", "name": "GST on Income", "rate": "10.000"}
_MOCK_TAX_CODES = {"items": [_MOCK_TAX_CODE], "total": 1, "page": 1, "page_size": 500}

_MOCK_CONTACT = {
    "id": _CONTACT_ID,
    "name": "Acme Pty Ltd",
    "contact_type": "CUSTOMER",
    "email": "billing@acme.example",
    "phone": "0400 000 001",
    "abn": "12 345 678 901",
    "address_line1": "Level 1, 123 Main St",
    "address_line2": None,
    "city": "Brisbane",
    "state": "QLD",
    "postcode": "4000",
    "country": "Australia",
    "notes": "Key account",
    "default_account_id": None,
    "default_tax_code": "GST",
    "bank_bsb": None,
    "bank_account_number": None,
    "bank_account_title": None,
    "company_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 5,
    "archived_at": None,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
}

# A newer server version returned after a 409 conflict.
_MOCK_CONTACT_V6 = {**_MOCK_CONTACT, "version": 6, "name": "Acme Pty Ltd (updated by someone else)"}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Edit form renders with the version hidden input
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_form_renders_with_version(respx_mock: respx.MockRouter) -> None:
    """GET /contacts/{id}/edit renders the form with a hidden version input + bank fields."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_CONTACT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with the correct value.
    assert 'name="version"' in resp.text
    assert 'value="5"' in resp.text
    # Form fields are pre-filled.
    assert "Acme Pty Ltd" in resp.text
    assert "Brisbane" in resp.text
    assert 'name="name"' in resp.text
    assert 'name="contact_type"' in resp.text
    # Bank fields present.
    assert 'name="bank_bsb"' in resp.text
    assert 'name="bank_account_number"' in resp.text
    assert 'name="bank_account_title"' in resp.text
    # Default account dropdown present.
    assert 'name="default_account_id"' in resp.text


# ---------------------------------------------------------------------------
# 2. Edit success → 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/{id}/edit with correct version; API 200 → 303 to detail."""
    respx_mock.patch(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_CONTACT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "Acme Pty Ltd",
                "contact_type": "CUSTOMER",
                "version": "5",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/contacts/{_CONTACT_ID}"


# ---------------------------------------------------------------------------
# 3. Edit sends If-Match header
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_sends_if_match_header(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/{id}/edit passes the version as the If-Match header."""
    captured_if_match: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured_if_match.append(request.headers.get("if-match", ""))
        return Response(200, json=_MOCK_CONTACT)

    respx_mock.patch(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "Acme Pty Ltd",
                "contact_type": "CUSTOMER",
                "version": "5",
            },
        )

    assert len(captured_if_match) == 1, "Expected exactly one upstream PATCH call"
    assert captured_if_match[0] == "5", f"Expected If-Match: 5, got {captured_if_match[0]!r}"


# ---------------------------------------------------------------------------
# 4. Edit conflict → banner + latest version from server
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 → re-render form with conflict banner."""
    respx_mock.patch(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # The route re-fetches the contact after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_CONTACT_V6)
    )
    # The re-render fetches accounts and tax_codes for the dropdowns.
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "My Updated Name",
                "contact_type": "CUSTOMER",
                "version": "5",  # stale
            },
        )

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else has updated this contact" in resp.text
    # Hidden version input updated to the server's latest version (6).
    assert 'value="6"' in resp.text
    # User's submitted name is preserved in the form.
    assert "My Updated Name" in resp.text


# ---------------------------------------------------------------------------
# 5. Edit validation error → re-render with field errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/{id}/edit where API returns 422 re-renders the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "contact_type"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.patch(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(422, json=_422_body)
    )
    # The re-render fetches accounts and tax_codes for the dropdowns.
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "No Type Corp",
                "version": "5",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered (not a blank page).
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "No Type Corp" in resp.text
    # Field error text visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 6. Edit not found — initial GET → 404
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_not_found(respx_mock: respx.MockRouter) -> None:
    """GET /contacts/{id}/edit when upstream returns 404 → HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(404, json={"detail": "Contact not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/edit")

    assert resp.status_code == 404
