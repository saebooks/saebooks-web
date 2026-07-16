"""Tests for the contacts detail view + create form — Lane D cycles 7, 43.

Nine tests:
1. test_contact_detail_renders              — GET /contacts/{id} shows contact name
2. test_contact_detail_404                  — upstream 404 propagates as HTTP 404
3. test_contact_new_form_renders            — GET /contacts/new returns form with fields + accounts dropdown
4. test_contact_create_success_redirects    — POST with valid body -> 303 to /contacts/{id}
5. test_contact_create_validation_error     — POST with bad body; 422 + error text visible
6. test_contact_create_sends_idempotency_key — POST includes X-Idempotency-Key header
7. test_contact_create_with_bank_fields     — POST with bank fields -> included in API payload
8. test_contact_create_with_default_account — POST with default_account_id -> included in payload
9. test_contact_new_form_accounts_dropdown  — GET /contacts/new with accounts -> select rendered
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

_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

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
    "company_id": "22222222-2222-2222-2222-222222222222",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 1,
    "archived_at": None,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Detail — renders contact name
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /contacts/{id} with a valid session renders the contact name."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_CONTACT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}")

    assert resp.status_code == 200
    assert "Acme Pty Ltd" in resp.text
    assert "Brisbane" in resp.text


# ---------------------------------------------------------------------------
# 2. Detail — upstream 404 propagates
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_detail_404(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(404, json={"detail": "Contact not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. New form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /contacts/new returns the form with all expected fields and bank fields."""
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
        resp = await client.get("/contacts/new")

    assert resp.status_code == 200
    # Required fields
    assert 'name="name"' in resp.text
    assert 'name="contact_type"' in resp.text
    # Optional fields
    assert 'name="email"' in resp.text
    assert 'name="phone"' in resp.text
    assert 'name="abn"' in resp.text
    assert 'name="address_line1"' in resp.text
    assert 'name="city"' in resp.text
    assert 'name="state"' in resp.text
    assert 'name="postcode"' in resp.text
    assert 'name="country"' in resp.text
    assert 'name="notes"' in resp.text
    assert 'name="default_tax_code"' in resp.text
    # Bank fields
    assert 'name="bank_bsb"' in resp.text
    assert 'name="bank_account_number"' in resp.text
    assert 'name="bank_account_title"' in resp.text
    # Default account dropdown
    assert 'name="default_account_id"' in resp.text
    # Idempotency key hidden input
    assert 'name="idempotency_key"' in resp.text


# ---------------------------------------------------------------------------
# 4. Create success -> redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(201, json=_MOCK_CONTACT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={
                "name": "Acme Pty Ltd",
                "contact_type": "CUSTOMER",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/contacts/{_CONTACT_ID}"


# ---------------------------------------------------------------------------
# 5. Create validation error -> re-render form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/new where API returns 422 re-renders the form with errors."""
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
    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(422, json=_422_body)
    )
    # The re-render path fetches accounts and tax_codes for the dropdowns.
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
            "/contacts/new",
            data={
                "name": "Missing Type Co",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert resp.status_code == 422
    # The form should be re-rendered, not a blank page.
    assert 'name="name"' in resp.text
    # The submitted name should be preserved.
    assert "Missing Type Co" in resp.text
    # The error message should appear.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 6. Create sends X-Idempotency-Key header
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_sends_idempotency_key(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/new passes the idempotency_key field as X-Idempotency-Key header."""
    _idem_key = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("x-idempotency-key", ""))
        return Response(201, json=_MOCK_CONTACT)

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/contacts/new",
            data={
                "name": "Idem Co",
                "contact_type": "SUPPLIER",
                "idempotency_key": _idem_key,
            },
        )

    assert len(captured) == 1, "Expected exactly one upstream POST call"
    assert captured[0] == _idem_key, f"Expected idempotency key {_idem_key!r}, got {captured[0]!r}"


# ---------------------------------------------------------------------------
# 7. Create with bank fields → included in API payload
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_with_bank_fields(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/new with bank_bsb/bank_account_number/bank_account_title sends them to API."""
    captured_bodies: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        import json as _j
        captured_bodies.append(_j.loads(request.content))
        return Response(201, json=_MOCK_CONTACT)

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/contacts/new",
            data={
                "name": "Bank Test Co",
                "contact_type": "SUPPLIER",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "bank_bsb": "062-000",
                "bank_account_number": "12345678",
                "bank_account_title": "Bank Test Co Pty Ltd",
            },
        )

    assert len(captured_bodies) == 1
    body = captured_bodies[0]
    assert body.get("bank_bsb") == "062-000"
    assert body.get("bank_account_number") == "12345678"
    assert body.get("bank_account_title") == "Bank Test Co Pty Ltd"


# ---------------------------------------------------------------------------
# 8. Create with default_account_id → included in API payload
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_with_default_account(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/new with default_account_id sends it to the API."""
    captured_bodies: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        import json as _j
        captured_bodies.append(_j.loads(request.content))
        return Response(201, json=_MOCK_CONTACT)

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/contacts/new",
            data={
                "name": "Account Test Co",
                "contact_type": "CUSTOMER",
                "idempotency_key": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "default_account_id": _ACCOUNT_ID,
            },
        )

    assert len(captured_bodies) == 1
    assert captured_bodies[0].get("default_account_id") == _ACCOUNT_ID


# ---------------------------------------------------------------------------
# 9. GET /contacts/new with accounts → select element rendered in form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_new_form_accounts_dropdown(respx_mock: respx.MockRouter) -> None:
    """GET /contacts/new with accounts available renders a select for default_account_id."""
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
        resp = await client.get("/contacts/new")

    assert resp.status_code == 200
    # The select element should be present (not the plain text fallback input).
    assert 'name="default_account_id"' in resp.text
    # The account option should appear — "4000 — Revenue".
    assert "4000" in resp.text
    assert "Revenue" in resp.text
    # Blank "None" option present.
    assert "— None —" in resp.text
