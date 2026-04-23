"""Tests for the recurring invoice create form — Lane D cycle 30.

Five tests:
1. test_ri_new_form_renders         — GET /recurring-invoices/new returns 200 with form
2. test_ri_add_line_htmx            — GET /recurring-invoices/_add_line returns line-row fragment
3. test_ri_create_success_redirects — POST valid payload; API 201 -> 303 to detail
4. test_ri_create_422_rerenders     — POST; API 422 -> 422 with form re-rendered and error
5. test_ri_frequency_in_form        — GET form includes frequency dropdown options
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

_RI_ID = "bbbbbbbb-bbbb-bbbb-bbbb-cc0000000030"
_CONTACT_ID = "cccccccc-cccc-cccc-cccc-cc0000000030"
_ACCOUNT_ID = "dddddddd-dddd-dddd-dddd-dd0000000030"
_TAX_CODE_ID = "eeeeeeee-eeee-eeee-eeee-ee0000000030"

_MOCK_CONTACT = {"id": _CONTACT_ID, "name": "Test Corp", "contact_type": "CUSTOMER"}
_MOCK_CONTACTS = {"items": [_MOCK_CONTACT], "total": 1, "limit": 200, "offset": 0}

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "name": "Services Revenue", "code": "4100", "account_type": "INCOME"}
_MOCK_ACCOUNTS = {"items": [_MOCK_ACCOUNT], "total": 1, "limit": 200, "offset": 0}

_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}
_MOCK_TAX_CODES = {"items": [_MOCK_TAX_CODE], "total": 1, "limit": 100, "offset": 0}

_MOCK_RI = {
    "id": _RI_ID,
    "company_id": "ffffffff-ffff-ffff-ffff-ff0000000030",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "name": "Monthly Retainer",
    "frequency": "MONTHLY",
    "status": "ACTIVE",
    "anchor_day": 1,
    "next_run": "2026-05-01",
    "end_date": None,
    "last_run": None,
    "due_days": 14,
    "payment_terms": "Net 14",
    "notes": None,
    "auto_post": False,
    "invoices_generated": 0,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "11111111-1111-1111-1111-110000000030",
            "line_no": 1,
            "description": "Monthly service fee",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "1.00",
            "unit_price": "500.00",
            "discount_pct": "0.00",
        }
    ],
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    """Register mock responses for the three dropdown-populating API calls."""
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
# 1. GET /recurring-invoices/new — form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices/new returns 200 with the create form."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/recurring-invoices/new")

    assert resp.status_code == 200
    # Required schedule fields present.
    assert 'name="name"' in resp.text
    assert 'name="contact_id"' in resp.text
    assert 'name="frequency"' in resp.text
    assert 'name="next_run"' in resp.text
    # Idempotency key hidden input.
    assert 'name="idempotency_key"' in resp.text
    # At least one line row rendered.
    assert 'name="lines[0][description]"' in resp.text
    # Dropdown options populated.
    assert "Test Corp" in resp.text
    assert "4100 — Services Revenue" in resp.text
    assert "GST" in resp.text


# ---------------------------------------------------------------------------
# 2. GET /recurring-invoices/_add_line — HTMX partial returns a line row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_add_line_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices/_add_line?index=2 returns the line-row fragment."""
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
        resp = await client.get("/recurring-invoices/_add_line?index=2")

    assert resp.status_code == 200
    # Must be a fragment, not a full page.
    assert "<html" not in resp.text
    # Must contain the correct index in field names.
    assert 'name="lines[2][description]"' in resp.text
    # Dropdown options populated.
    assert "4100 — Services Revenue" in resp.text


# ---------------------------------------------------------------------------
# 3. POST /recurring-invoices/new — success redirects to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /recurring-invoices/new with valid data; API 201 -> 303 to detail."""
    respx_mock.post(f"{_API_BASE}/api/v1/recurring_invoices").mock(
        return_value=Response(201, json=_MOCK_RI)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/recurring-invoices/new",
            data={
                "name": "Monthly Retainer",
                "contact_id": _CONTACT_ID,
                "frequency": "MONTHLY",
                "next_run": "2026-05-01",
                "due_days": "14",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aa0000000030",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Monthly service fee",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "500.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/recurring-invoices/{_RI_ID}"


# ---------------------------------------------------------------------------
# 4. POST /recurring-invoices/new — API 422 re-renders form with errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_create_422_rerenders(respx_mock: respx.MockRouter) -> None:
    """POST; upstream 422 -> re-render the form with errors at status 422."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "contact_id"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/recurring_invoices").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/recurring-invoices/new",
            data={
                "name": "Monthly Retainer",
                "frequency": "MONTHLY",
                "next_run": "2026-05-01",
                "due_days": "14",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bb0000000030",
                "lines[0][description]": "Monthly service fee",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "500.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — required fields still present.
    assert 'name="contact_id"' in resp.text
    # Error message should appear.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 5. GET form — frequency dropdown options rendered
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_frequency_in_form(respx_mock: respx.MockRouter) -> None:
    """GET /recurring-invoices/new includes all frequency dropdown options."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/recurring-invoices/new")

    assert resp.status_code == 200
    # All five frequencies must be present in the dropdown.
    assert "WEEKLY" in resp.text
    assert "FORTNIGHTLY" in resp.text
    assert "MONTHLY" in resp.text
    assert "QUARTERLY" in resp.text
    assert "YEARLY" in resp.text
