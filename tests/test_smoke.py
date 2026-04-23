"""Smoke tests for the saebooks-web frontend.

Test 1: /healthz returns 200.
Test 2: GET /contacts with a valid session token renders a contacts row
        when the upstream API returns a mocked response.

The upstream saebooks-api is fully mocked via respx so these tests run
without a live API or database.
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks_web.main import app
from saebooks_web.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_CONTACT = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Acme Pty Ltd",
    "contact_type": "customer",
    "email": "billing@acme.example",
    "phone": "0400 000 001",
    "abn": None,
    "address_line1": None,
    "address_line2": None,
    "city": "Brisbane",
    "state": "QLD",
    "postcode": "4000",
    "country": "Australia",
    "notes": None,
    "default_account_id": None,
    "default_tax_code": None,
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

_MOCK_CONTACTS_RESPONSE = {
    "items": [_MOCK_CONTACT],
    "total": 1,
    "limit": 100,
    "offset": 0,
}

# We need a real session cookie to test authenticated routes.  Build one using
# itsdangerous exactly as Starlette's SessionMiddleware does:
#   data = b64encode(json.dumps(session).encode("utf-8"))
#   cookie_value = signer.sign(data).decode("utf-8")
import json as _json
from base64 import b64encode as _b64encode

from itsdangerous import TimestampSigner as _TimestampSigner


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_healthz_returns_200() -> None:
    """The /healthz endpoint is always 200, no auth required."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.anyio
@respx.mock
async def test_contacts_page_renders_row(respx_mock: respx.MockRouter) -> None:
    """GET /contacts with a valid session renders the mocked contact name."""
    # Mock the API token-validation probe that happens on login (not needed
    # here since we inject the session directly) AND the contacts list call.
    api_base = settings.api_url.rstrip("/")

    respx_mock.get(f"{api_base}/api/v1/contacts").mock(
        return_value=Response(200, json=_MOCK_CONTACTS_RESPONSE)
    )

    # Build a signed session cookie that looks like a logged-in session.
    session_data = {"api_token": "test-token-abc"}
    cookie_value = _make_session_cookie(session_data)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: cookie_value},
    ) as client:
        resp = await client.get("/contacts")

    assert resp.status_code == 200
    assert "Acme Pty Ltd" in resp.text
    assert "Brisbane" in resp.text
