"""Tests for the credit note archive action — Lane D cycle 20.

Four tests:
1. test_credit_note_archive_happy_path    — API 204 -> 303 to /credit-notes with flash
2. test_credit_note_archive_conflict      — API 409 -> 303 back to detail
3. test_credit_note_archive_gate_failure  — API 422 -> 303 back to detail
4. test_credit_note_archive_button_hidden — POSTED credit note detail has no archive form
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

_CN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"

_MOCK_CN_DRAFT = {
    "id": _CN_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "CN-0001",
    "issue_date": "2026-04-23",
    "status": "DRAFT",
    "reason": None,
    "original_invoice_id": None,
    "subtotal": "50.00",
    "tax_total": "5.00",
    "total": "55.00",
    "amount_allocated": "0.00",
    "notes": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_CN_POSTED = {**_MOCK_CN_DRAFT, "status": "POSTED", "version": 2}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


@pytest.mark.anyio
@respx.mock
async def test_credit_note_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /credit-notes/{id}/archive; API 204 -> 303 to /credit-notes with flash."""
    respx_mock.delete(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(204)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/credit-notes"


@pytest.mark.anyio
@respx.mock
async def test_credit_note_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 back to credit note detail."""
    respx_mock.delete(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/archive",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"


@pytest.mark.anyio
@respx.mock
async def test_credit_note_archive_gate_failure(respx_mock: respx.MockRouter) -> None:
    """API 422 -> 303 back to credit note detail with API message."""
    respx_mock.delete(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(
            422, json={"detail": "Cannot archive a POSTED credit note."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/archive",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"


@pytest.mark.anyio
@respx.mock
async def test_credit_note_archive_button_hidden_when_posted(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a POSTED credit note must not render the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}")

    assert resp.status_code == 200
    assert f"/credit-notes/{_CN_ID}/archive" not in resp.text
