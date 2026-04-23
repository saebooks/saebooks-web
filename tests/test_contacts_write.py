"""Tests for the contacts detail view + create form — Lane D cycle 7.

Six tests:
1. test_contact_detail_renders         — GET /contacts/{id} shows contact name
2. test_contact_detail_404             — upstream 404 propagates as HTTP 404
3. test_contact_new_form_renders       — GET /contacts/new returns form with fields
4. test_contact_create_success_redirects — POST with valid body -> 303 to /contacts/{id}
5. test_contact_create_validation_error  — POST with bad body; 422 + error text visible
6. test_contact_create_sends_idempotency_key — POST includes X-Idempotency-Key header
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
async def test_contact_new_form_renders() -> None:
    """GET /contacts/new returns the form with all expected fields."""
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
