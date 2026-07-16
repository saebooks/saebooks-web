"""Tests for the contact archive action — Lane D cycle 20.

Contacts have no status field — archive is available whenever archived_at is None.

Four tests:
1. test_contact_archive_happy_path       — API 204 -> 303 to /contacts with flash
2. test_contact_archive_conflict         — API 409 -> 303 back to detail
3. test_contact_archive_gate_failure     — API 422 -> 303 back to detail
4. test_contact_archive_button_hidden    — already-archived contact has no archive form

The contacts detail template shows the Archive button when contact.archived_at is falsy.
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

_CONTACT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

_MOCK_CONTACT_ACTIVE = {
    "id": _CONTACT_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "name": "Acme Pty Ltd",
    "contact_type": "CUSTOMER",
    "email": "acme@example.com",
    "phone": None,
    "abn": None,
    "address_line1": None,
    "address_line2": None,
    "city": None,
    "state": None,
    "postcode": None,
    "country": None,
    "notes": None,
    "default_tax_code": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
}

_MOCK_CONTACT_ARCHIVED = {
    **_MOCK_CONTACT_ACTIVE,
    "archived_at": "2026-04-23T10:00:00Z",
    "version": 2,
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


@pytest.mark.anyio
@respx.mock
async def test_contact_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /contacts/{id}/archive; API 204 -> 303 to /contacts with flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect) — mock the contacts list API call
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/contacts"


@pytest.mark.anyio
@respx.mock
async def test_contact_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 back to contact detail."""
    respx_mock.delete(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/archive",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/contacts/{_CONTACT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_contact_archive_gate_failure(respx_mock: respx.MockRouter) -> None:
    """API 422 -> 303 back to contact detail with API message in flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(422, json={"detail": "Contact is already archived."})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/archive",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/contacts/{_CONTACT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_contact_archive_button_hidden_when_already_archived(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for an already-archived contact must not show the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_CONTACT_ARCHIVED)
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
    # Archive form must not be shown for an already-archived contact.
    assert f"/contacts/{_CONTACT_ID}/archive" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_contact_archive_button_shown_when_active(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for an active contact shows the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_MOCK_CONTACT_ACTIVE)
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
    # Archive form IS shown for an active (non-archived) contact.
    assert f"/contacts/{_CONTACT_ID}/archive" in resp.text
