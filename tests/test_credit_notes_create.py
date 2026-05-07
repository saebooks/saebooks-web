"""Tests for the credit note create form — Lane D cycle 14.

Six tests:
1. test_credit_note_new_requires_auth         — GET /credit-notes/new without session -> 303 /login
2. test_credit_note_new_form_renders          — GET /credit-notes/new returns 200 with form + starter line
3. test_credit_note_add_line_fragment         — GET /credit-notes/_add_line returns <tr> without <html>
4. test_credit_note_create_success_redirects  — POST with valid payload; mock API 201; expect 303 to /credit-notes/{id}
5. test_credit_note_create_validation_error   — API 422; form re-renders with user input preserved
6. test_credit_note_create_sends_idempotency_key — API call received X-Idempotency-Key

Key differences from invoice/bill tests:
- No due_date in form payload (CreditNoteCreate has none)
- reason and original_invoice_id are optional credit-note-specific fields
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

_CN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"
_INVOICE_ID = "44444444-4444-4444-4444-444444444444"

_MOCK_CUSTOMER = {"id": _CONTACT_ID, "name": "Acme Pty Ltd", "contact_type": "CUSTOMER"}
_MOCK_CONTACTS = {"items": [_MOCK_CUSTOMER], "total": 1, "limit": 200, "offset": 0}

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "name": "Revenue", "code": "4000", "account_type": "INCOME"}
_MOCK_ACCOUNTS = {"items": [_MOCK_ACCOUNT], "total": 1, "limit": 200, "offset": 0}

_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}
_MOCK_TAX_CODES = {"items": [_MOCK_TAX_CODE], "total": 1, "limit": 100, "offset": 0}

_MOCK_CREDIT_NOTE = {
    "id": _CN_ID,
    "company_id": "55555555-5555-5555-5555-555555555555",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "CN-0001",
    "issue_date": "2026-04-23",
    "status": "DRAFT",
    "original_invoice_id": _INVOICE_ID,
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "amount_allocated": "0.00",
    "reason": "Overcharge correction",
    "notes": None,
    "posted_at": None,
    "posted_by": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "66666666-6666-6666-6666-666666666666",
            "line_no": 1,
            "description": "Consulting adjustment",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "1.0",
            "unit_price": "100.00",
            "discount_pct": "0.0",
            "line_subtotal": "100.00",
            "line_tax": "10.00",
            "line_total": "110.00",
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
# 1. GET /credit-notes/new — requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_credit_note_new_requires_auth() -> None:
    """GET /credit-notes/new without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/credit-notes/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. GET /credit-notes/new — form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /credit-notes/new returns 200 with the create form and a starter line row."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/credit-notes/new")

    assert resp.status_code == 200
    # Required fields present
    assert 'name="contact_id"' in resp.text
    assert 'name="issue_date"' in resp.text
    # Credit-note-specific optional fields
    assert 'name="reason"' in resp.text
    assert 'name="original_invoice_id"' in resp.text
    # Idempotency key hidden input
    assert 'name="idempotency_key"' in resp.text
    # At least one line row rendered
    assert 'name="lines[0][description]"' in resp.text
    # Dropdown options populated
    assert "Acme Pty Ltd" in resp.text
    assert "4000 — Revenue" in resp.text
    assert "GST" in resp.text


# ---------------------------------------------------------------------------
# 3. GET /credit-notes/_add_line — HTMX partial returns a line row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_add_line_fragment(respx_mock: respx.MockRouter) -> None:
    """GET /credit-notes/_add_line?index=1 returns the line-row fragment, not a full page."""
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
        resp = await client.get("/credit-notes/_add_line?index=1")

    assert resp.status_code == 200
    # Must be a fragment, not a full page.
    assert "<html" not in resp.text
    # Must contain the correct index in field names.
    assert 'name="lines[1][description]"' in resp.text
    # Dropdown options populated.
    assert "4000 — Revenue" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /credit-notes/new — success redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /credit-notes/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes").mock(
        return_value=Response(201, json=_MOCK_CREDIT_NOTE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/credit-notes/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "reason": "Overcharge correction",
                "original_invoice_id": _INVOICE_ID,
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting adjustment",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"


# ---------------------------------------------------------------------------
# 5. POST /credit-notes/new — validation error re-renders form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST without contact_id: upstream 422 -> re-render the form with errors."""
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
    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes").mock(
        return_value=Response(422, json=_422_body)
    )
    # Dropdown re-population after validation failure also needs mocking.
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/credit-notes/new",
            data={
                "issue_date": "2026-04-23",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "lines[0][description]": "Consulting adjustment",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — required fields still present.
    assert 'name="contact_id"' in resp.text
    # Error message should appear.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 6. POST /credit-notes/new — X-Idempotency-Key header forwarded
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_create_sends_idempotency_key(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /credit-notes/new passes the idempotency_key field as X-Idempotency-Key header."""
    _idem_key = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("x-idempotency-key", ""))
        return Response(201, json=_MOCK_CREDIT_NOTE)

    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/credit-notes/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "idempotency_key": _idem_key,
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Consulting adjustment",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert len(captured) == 1, "Expected exactly one upstream POST call"
    assert captured[0] == _idem_key, f"Expected {_idem_key!r}, got {captured[0]!r}"


# ---------------------------------------------------------------------------
# 7. GET /credit-notes/new — reason code dropdown with standard codes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_new_reason_code_dropdown(respx_mock: respx.MockRouter) -> None:
    """GET /credit-notes/new includes a reason code <select> with all standard codes."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/credit-notes/new")

    assert resp.status_code == 200
    # Dropdown must be a <select> element, not a free-text input.
    assert '<select' in resp.text
    assert 'name="reason"' in resp.text
    # All five standard reason codes must appear as options.
    for code in (
        "Return of goods",
        "Change of terms",
        "GST registration change",
        "Error correction",
        "Other",
    ):
        assert code in resp.text, f"Missing reason code option: {code!r}"
    # Guidance callout must be present.
    assert "GST registration change" in resp.text
    assert "re-issue" in resp.text.lower() or "re-invoice" in resp.text.lower() or "re-issued" in resp.text.lower()
