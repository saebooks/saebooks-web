"""Tests for the recurring invoice archive action — Lane D cycle 30.

Three tests:
1. test_ri_archive_happy          — POST /recurring-invoices/{id}/archive; API 204 -> 303 to list with flash
2. test_ri_archive_conflict       — API 409 -> 303 back to detail
3. test_ri_archive_button_guard   — detail page for archived RI has no archive form
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
# Constants / mock data
# ---------------------------------------------------------------------------

_RI_ID = "bbbbbbbb-bbbb-bbbb-bbbb-ar0000000030"
_CONTACT_ID = "cccccccc-cccc-cccc-cccc-ar0000000030"

_MOCK_RI_ACTIVE = {
    "id": _RI_ID,
    "company_id": "ffffffff-ffff-ffff-ffff-ar0000000030",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "name": "Archive Test RI",
    "frequency": "MONTHLY",
    "status": "ACTIVE",
    "anchor_day": None,
    "next_run": "2026-05-01",
    "end_date": None,
    "last_run": None,
    "due_days": 30,
    "payment_terms": None,
    "notes": None,
    "auto_post": False,
    "invoices_generated": 1,
    "version": 2,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

# Already-archived RI — no action buttons should appear.
_MOCK_RI_ARCHIVED = {
    **_MOCK_RI_ACTIVE,
    "archived_at": "2026-04-24T12:00:00Z",
    "version": 3,
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Happy path — 204 -> 303 to list with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_archive_happy(respx_mock: respx.MockRouter) -> None:
    """POST /recurring-invoices/{id}/archive; API 204 -> 303 redirect to /recurring-invoices."""
    respx_mock.delete(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(204)
    )
    # List page GET (after redirect) — mock the recurring invoices list API call.
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/archive",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/recurring-invoices"


# ---------------------------------------------------------------------------
# 2. Conflict — 409 -> 303 back to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_archive_conflict(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail page."""
    respx_mock.delete(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/archive",
            data={"version": "1"},  # stale
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/recurring-invoices/{_RI_ID}"


# ---------------------------------------------------------------------------
# 3. Archive button guard — detail page for archived RI has no archive form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_archive_button_guard(respx_mock: respx.MockRouter) -> None:
    """Detail page for an already-archived RI must not render the archive form."""
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(200, json=_MOCK_RI_ARCHIVED)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json={"id": _CONTACT_ID, "name": "Archive Guard Contact"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/recurring-invoices/{_RI_ID}")

    assert resp.status_code == 200
    # Archive form must not be present for an already-archived RI.
    assert f"/recurring-invoices/{_RI_ID}/archive" not in resp.text
    # Edit button must also be absent.
    assert f"/recurring-invoices/{_RI_ID}/edit" not in resp.text
