"""Tests for the invoice edit form — Lane D cycle 12.

Eight tests:
1. test_edit_requires_auth              — GET /invoices/{id}/edit without session -> 303 /login
2. test_edit_form_renders_for_draft     — mock DRAFT invoice -> form with version hidden input + lines
3. test_edit_blocked_for_posted         — mock POSTED invoice -> blocked page (422), no form
4. test_edit_success_redirects          — POST with valid body; mock PATCH 200 -> 303 to detail
5. test_edit_sends_if_match_header      — assert outbound PATCH included If-Match: <version>
6. test_edit_conflict_shows_banner      — mock PATCH 409 + re-GET -> amber banner + new version
7. test_edit_validation_error           — mock PATCH 422 -> form re-renders with field errors
8. test_edit_parses_lines_replacement   — 3 lines submitted; assert PATCH body has lines:[{...}*3]
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

_INVOICE_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"

_MOCK_CONTACT = {"id": _CONTACT_ID, "name": "Acme Pty Ltd", "contact_type": "CUSTOMER"}
_MOCK_CONTACTS = {"items": [_MOCK_CONTACT], "total": 1, "limit": 200, "offset": 0}

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "name": "Revenue", "code": "4000", "account_type": "INCOME"}
_MOCK_ACCOUNTS = {"items": [_MOCK_ACCOUNT], "total": 1, "limit": 200, "offset": 0}

_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}
_MOCK_TAX_CODES = {"items": [_MOCK_TAX_CODE], "total": 1, "limit": 100, "offset": 0}

_MOCK_INVOICE_DRAFT = {
    "id": _INVOICE_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0001",
    "issue_date": "2026-04-23",
    "due_date": "2026-05-23",
    "status": "DRAFT",
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "notes": None,
    "payment_terms": "Net 30",
    "posted_at": None,
    "posted_by": None,
    "version": 3,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "line_no": 1,
            "description": "Consulting",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "1.0",
            "unit_price": "100.00",
            "discount_pct": "0.0",
            "line_subtotal": "100.00",
            "line_tax": "10.00",
            "line_total": "110.00",
            "project_id": None,
            "item_id": None,
        }
    ],
}

_MOCK_INVOICE_POSTED = {**_MOCK_INVOICE_DRAFT, "status": "POSTED", "version": 4}

# A newer server version returned after a 409 conflict.
_MOCK_INVOICE_V4 = {**_MOCK_INVOICE_DRAFT, "version": 4, "notes": "Updated by someone else"}

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
# 1. Edit requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_edit_requires_auth() -> None:
    """GET /invoices/{id}/edit without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}/edit")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Edit form renders for DRAFT invoice
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_form_renders_for_draft(respx_mock: respx.MockRouter) -> None:
    """GET /invoices/{id}/edit for a DRAFT invoice renders the edit form."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_DRAFT)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with correct value.
    assert 'name="version"' in resp.text
    assert 'value="3"' in resp.text
    # Idempotency key input present.
    assert 'name="idempotency_key"' in resp.text
    # Existing lines visible in form.
    assert 'name="lines[0][description]"' in resp.text
    assert "Consulting" in resp.text
    # Header fields pre-filled.
    assert 'name="issue_date"' in resp.text
    assert "2026-04-23" in resp.text


# ---------------------------------------------------------------------------
# 3. Edit blocked for POSTED invoice
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_blocked_for_posted(respx_mock: respx.MockRouter) -> None:
    """GET /invoices/{id}/edit for a POSTED invoice shows blocked page, not the form."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}/edit")

    assert resp.status_code == 422
    # Must NOT render the edit form.
    assert 'name="version"' not in resp.text
    assert 'name="issue_date"' not in resp.text
    # Must show the blocked message.
    assert "cannot be edited" in resp.text


# ---------------------------------------------------------------------------
# 4. Edit success redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/{id}/edit with valid body; API 200 -> 303 to detail page."""
    respx_mock.patch(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_DRAFT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/edit",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "version": "3",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 5. Edit sends If-Match header
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_sends_if_match_header(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/{id}/edit passes the version as the If-Match header."""
    captured_if_match: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured_if_match.append(request.headers.get("if-match", ""))
        return Response(200, json=_MOCK_INVOICE_DRAFT)

    respx_mock.patch(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            f"/invoices/{_INVOICE_ID}/edit",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "version": "3",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert len(captured_if_match) == 1, "Expected exactly one upstream PATCH call"
    assert captured_if_match[0] == "3", f"Expected If-Match: 3, got {captured_if_match[0]!r}"


# ---------------------------------------------------------------------------
# 6. Edit conflict shows banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> re-render form with conflict banner + new version."""
    respx_mock.patch(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # The route re-fetches the invoice after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE_V4)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/edit",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "notes": "My updated notes",
                "version": "3",  # stale
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else updated this invoice" in resp.text
    # Hidden version input updated to the server's latest version (4).
    assert 'value="4"' in resp.text
    # User's submitted notes preserved.
    assert "My updated notes" in resp.text


# ---------------------------------------------------------------------------
# 7. Edit validation error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /invoices/{id}/edit where API returns 422 re-renders form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "issue_date"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.patch(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/edit",
            data={
                "contact_id": _CONTACT_ID,
                "due_date": "2026-05-23",
                "version": "3",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered with fields still present.
    assert 'name="due_date"' in resp.text
    # Field error visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 8. Edit parses lines for full replacement
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_edit_parses_lines_replacement(respx_mock: respx.MockRouter) -> None:
    """POST with 3 lines; assert the PATCH body has lines:[{...}, {...}, {...}]."""
    captured_bodies: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured_bodies.append(request.content)
        return Response(200, json=_MOCK_INVOICE_DRAFT)

    respx_mock.patch(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(side_effect=_capture)

    _ACCOUNT_ID2 = "66666666-6666-6666-6666-666666666666"
    _ACCOUNT_ID3 = "77777777-7777-7777-7777-777777777777"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            f"/invoices/{_INVOICE_ID}/edit",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "version": "3",
                "idempotency_key": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                # Line 0
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting",
                "lines[0][quantity]": "2",
                "lines[0][unit_price]": "50.00",
                # Line 1
                "lines[1][account_id]": _ACCOUNT_ID2,
                "lines[1][description]": "Travel",
                "lines[1][quantity]": "1",
                "lines[1][unit_price]": "200.00",
                # Line 2
                "lines[2][account_id]": _ACCOUNT_ID3,
                "lines[2][description]": "Materials",
                "lines[2][quantity]": "3",
                "lines[2][unit_price]": "30.00",
            },
        )

    assert len(captured_bodies) == 1, "Expected exactly one upstream PATCH call"
    body = _json.loads(captured_bodies[0])
    lines = body.get("lines", [])
    assert len(lines) == 3, f"Expected 3 lines in PATCH body, got {len(lines)}: {lines}"
    assert lines[0]["description"] == "Consulting"
    assert lines[1]["description"] == "Travel"
    assert lines[2]["description"] == "Materials"
    assert lines[0]["unit_price"] == "50.00"
    assert lines[1]["unit_price"] == "200.00"
    assert lines[2]["unit_price"] == "30.00"
