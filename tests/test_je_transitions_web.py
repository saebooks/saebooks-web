"""Tests for journal entry POST and REVERSE transition actions — Lane D cycle 37.

Six tests:
1. test_je_post_success_redirects              — POST /journal-entries/{id}/post; API 200 -> 303 to detail
2. test_je_post_409_shows_flash                — stale version -> API 409 -> 303 with conflict flash
3. test_je_post_422_shows_flash                — API 422 -> 303 with API error message as flash
4. test_je_reverse_success_redirects_to_reversal — POST /journal-entries/{id}/reverse; API 201 -> 303 to reversal entry
5. test_je_reverse_409_shows_flash             — API 409 -> 303 back to original entry with flash
6. test_je_post_button_not_shown_for_posted    — Post button absent on POSTED JE detail page
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

_JE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_REVERSAL_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

_MOCK_JE_DRAFT = {
    "id": _JE_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000010",
    "entry_date": "2026-04-24",
    "status": "DRAFT",
    "description": "Test JE",
    "reference": None,
    "posted_at": None,
    "posted_by": None,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_JE_POSTED = {
    **_MOCK_JE_DRAFT,
    "status": "POSTED",
    "posted_at": "2026-04-24T01:00:00Z",
    "version": 2,
}

_MOCK_JE_REVERSAL = {
    "id": _REVERSAL_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000011",
    "entry_date": "2026-04-24",
    "status": "DRAFT",
    "description": "Reversal of JE-000010",
    "reference": None,
    "posted_at": None,
    "posted_by": None,
    "version": 1,
    "created_at": "2026-04-24T02:00:00Z",
    "updated_at": "2026-04-24T02:00:00Z",
    "archived_at": None,
    "lines": [],
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Happy path — POST /journal-entries/{id}/post; API 200 -> 303 to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_post_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /journal-entries/{id}/post; API 200 -> 303 redirect to detail."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/post").mock(
        return_value=Response(200, json=_MOCK_JE_POSTED)
    )
    # Mock detail GET so a follow_redirects=True client can render the page.
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"


# ---------------------------------------------------------------------------
# 2. Stale version — API 409 -> 303 back to detail with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_post_409_shows_flash(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/post").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"

    # Follow the redirect and verify the flash text appears in the rendered page.
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_DRAFT)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "0"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 3. Validation error — API 422 -> 303 back to detail with API error as flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_post_422_shows_flash(respx_mock: respx.MockRouter) -> None:
    """API 422 business-rule rejection -> 303 back to detail with message as flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/post").mock(
        return_value=Response(
            422, json={"detail": "Journal entry has no lines and cannot be posted."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"

    # Follow the redirect and verify the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_DRAFT)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "1"},
        )
    assert "Journal entry has no lines" in resp2.text


# ---------------------------------------------------------------------------
# 4. Happy path — reverse; API 201 -> 303 redirect to new reversal entry
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_reverse_success_redirects_to_reversal(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /journal-entries/{id}/reverse; API 201 -> 303 redirect to new reversal entry."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/reverse").mock(
        return_value=Response(201, json=_MOCK_JE_REVERSAL)
    )
    # Mock detail GET for the reversal entry (in case follow_redirects=True).
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_REVERSAL_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_REVERSAL)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/reverse",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_REVERSAL_ID}"


# ---------------------------------------------------------------------------
# 5. Reverse conflict — API 409 -> 303 back to original entry with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_reverse_409_shows_flash(respx_mock: respx.MockRouter) -> None:
    """POST /journal-entries/{id}/reverse with stale version; API 409 -> 303 back to original."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/reverse").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/reverse",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"

    # Follow the redirect and verify the conflict flash appears.
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_POSTED)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/journal-entries/{_JE_ID}/reverse",
            data={"version": "1"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 6. Post button absent on POSTED JE; Reverse button present
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_post_button_not_shown_for_posted(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a POSTED JE must not render the Post form but must render the Reverse form."""
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
    # Post and archive buttons must not appear for POSTED entries.
    assert f"/journal-entries/{_JE_ID}/post" not in resp.text
    assert f"/journal-entries/{_JE_ID}/archive" not in resp.text
    # Reverse button must be present for POSTED entries.
    assert f"/journal-entries/{_JE_ID}/reverse" in resp.text
