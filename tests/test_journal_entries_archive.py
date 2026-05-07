"""Tests for the journal entry archive action — Lane D cycle 20.

Four tests:
1. test_journal_entry_archive_happy_path    — API 204 -> 303 to /journal-entries with flash
2. test_journal_entry_archive_conflict      — API 409 -> 303 back to detail
3. test_journal_entry_archive_gate_failure  — API 422 -> 303 back to detail
4. test_journal_entry_archive_button_hidden — POSTED JE detail has no archive form
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

_JE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_MOCK_JE_DRAFT = {
    "id": _JE_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000001",
    "entry_date": "2026-04-23",
    "status": "DRAFT",
    "description": "Test entry",
    "reference": None,
    "posted_at": None,
    "posted_by": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_JE_POSTED = {**_MOCK_JE_DRAFT, "status": "POSTED", "version": 2}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_archive_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /journal-entries/{id}/archive; API 204 -> 303 to /journal-entries."""
    respx_mock.delete(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(204)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/archive",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/journal-entries"


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """API 409 -> 303 back to journal entry detail."""
    respx_mock.delete(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/archive",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_archive_gate_failure(respx_mock: respx.MockRouter) -> None:
    """API 422 -> 303 back to journal entry detail with flash message."""
    respx_mock.delete(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(
            422, json={"detail": "Cannot archive a POSTED journal entry."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/archive",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_archive_button_hidden_when_posted(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a POSTED journal entry must not render the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/journal-entries/{_JE_ID}")

    assert resp.status_code == 200
    assert f"/journal-entries/{_JE_ID}/archive" not in resp.text
