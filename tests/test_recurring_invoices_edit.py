"""Tests for the recurring invoice edit form — Lane D cycle 30.

Five tests:
1. test_ri_edit_active_form_renders  — GET /recurring-invoices/{id}/edit for ACTIVE -> form with version
2. test_ri_edit_ended_blocked        — GET for ENDED -> blocked page (422), no form
3. test_ri_edit_success_redirects    — POST valid; API 200 -> 303 to detail
4. test_ri_edit_conflict_shows_banner — POST; API 409 -> amber banner + new version
5. test_ri_edit_validation_error     — POST; API 422 -> form re-renders with errors
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

_RI_ID = "bbbbbbbb-bbbb-bbbb-bbbb-ed0000000030"
_CONTACT_ID = "cccccccc-cccc-cccc-cccc-ed0000000030"
_ACCOUNT_ID = "dddddddd-dddd-dddd-dddd-ed0000000030"

_MOCK_CONTACT = {"id": _CONTACT_ID, "name": "Edit Corp", "contact_type": "CUSTOMER"}
_MOCK_CONTACTS = {"items": [_MOCK_CONTACT], "total": 1, "limit": 200, "offset": 0}
_MOCK_ACCOUNTS = {"items": [], "total": 0, "limit": 200, "offset": 0}
_MOCK_TAX_CODES = {"items": [], "total": 0, "limit": 100, "offset": 0}

_MOCK_RI_ACTIVE = {
    "id": _RI_ID,
    "company_id": "ffffffff-ffff-ffff-ffff-ed0000000030",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "name": "Quarterly Report",
    "frequency": "QUARTERLY",
    "status": "ACTIVE",
    "anchor_day": 1,
    "next_run": "2026-07-01",
    "end_date": None,
    "last_run": None,
    "due_days": 30,
    "payment_terms": "Net 30",
    "notes": "Quarterly review invoice",
    "auto_post": False,
    "invoices_generated": 2,
    "version": 3,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "11111111-1111-1111-1111-ed0000000030",
            "line_no": 1,
            "description": "Quarterly consulting",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": None,
            "quantity": "1.00",
            "unit_price": "1200.00",
            "discount_pct": "0.00",
        }
    ],
}

_MOCK_RI_ENDED = {**_MOCK_RI_ACTIVE, "status": "ENDED", "version": 5}

# A newer server version returned after a 409 conflict.
_MOCK_RI_V4 = {**_MOCK_RI_ACTIVE, "version": 4, "notes": "Updated elsewhere"}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json=_MOCK_CONTACTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )


# ---------------------------------------------------------------------------
# 1. GET edit — ACTIVE invoice renders form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_edit_active_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices/{id}/edit for ACTIVE invoice renders the edit form."""
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(200, json=_MOCK_RI_ACTIVE)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/recurring-invoices/{_RI_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with correct value.
    assert 'name="version"' in resp.text
    assert 'value="3"' in resp.text
    # Idempotency key input present.
    assert 'name="idempotency_key"' in resp.text
    # Schedule fields pre-filled.
    assert 'name="next_run"' in resp.text
    assert "2026-07-01" in resp.text
    assert 'name="frequency"' in resp.text
    # Existing lines visible in form.
    assert 'name="lines[0][description]"' in resp.text
    assert "Quarterly consulting" in resp.text


# ---------------------------------------------------------------------------
# 2. GET edit — ENDED invoice shows blocked page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_edit_ended_blocked(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices/{id}/edit for ENDED invoice shows blocked page, not form."""
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(200, json=_MOCK_RI_ENDED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/recurring-invoices/{_RI_ID}/edit")

    assert resp.status_code == 422
    # Must NOT render the edit form.
    assert 'name="version"' not in resp.text
    assert 'name="next_run"' not in resp.text
    # Must show the blocked message.
    assert "cannot be edited" in resp.text


# ---------------------------------------------------------------------------
# 3. POST edit — success redirects to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /recurring-invoices/{id}/edit valid; API 200 -> 303 to detail page."""
    respx_mock.patch(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(200, json=_MOCK_RI_ACTIVE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/edit",
            data={
                "name": "Quarterly Report",
                "contact_id": _CONTACT_ID,
                "frequency": "QUARTERLY",
                "next_run": "2026-07-01",
                "due_days": "30",
                "version": "3",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-ed0000000030",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Quarterly consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "1200.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/recurring-invoices/{_RI_ID}"


# ---------------------------------------------------------------------------
# 4. POST edit — 409 conflict shows banner with new version
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> re-render form with conflict banner + new version."""
    respx_mock.patch(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # The route re-fetches the RI after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(200, json=_MOCK_RI_V4)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/edit",
            data={
                "name": "Quarterly Report",
                "contact_id": _CONTACT_ID,
                "frequency": "QUARTERLY",
                "next_run": "2026-07-01",
                "notes": "My changes",
                "due_days": "30",
                "version": "3",  # stale
                "idempotency_key": "dddddddd-dddd-dddd-dddd-ed0000000030",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Quarterly consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "1200.00",
            },
        )

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else updated this recurring invoice" in resp.text
    # Hidden version input updated to server's latest version (4).
    assert 'value="4"' in resp.text
    # User's submitted notes preserved.
    assert "My changes" in resp.text


# ---------------------------------------------------------------------------
# 5. POST edit — 422 validation error re-renders form with errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /recurring-invoices/{id}/edit; API 422 -> re-render form with field errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "next_run"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.patch(f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/edit",
            data={
                "name": "Quarterly Report",
                "contact_id": _CONTACT_ID,
                "frequency": "QUARTERLY",
                "due_days": "30",
                "version": "3",
                "idempotency_key": "eeeeeeee-eeee-eeee-eeee-ed0000000030",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Quarterly consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "1200.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered with fields still present.
    assert 'name="frequency"' in resp.text
    # Field error visible.
    assert "Field required" in resp.text
