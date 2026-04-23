"""Tests for the journal entry create form — Lane D cycle 16.

Seven tests:
1. test_journal_entry_new_requires_auth         — GET /journal-entries/new without session -> 303 /login
2. test_journal_entry_new_form_renders          — GET /journal-entries/new returns 200 with form + two starter lines
3. test_journal_entry_add_line_fragment         — GET /journal-entries/_add_line returns <tr> without <html>
4. test_journal_entry_create_success_redirects  — POST balanced entry; mock API 201; expect 303 to /journal-entries/{id}
5. test_journal_entry_create_unbalanced_422     — API 422 (unbalanced); form re-renders with API error message
6. test_journal_entry_create_validation_error   — API 422 (missing entry_date); form re-renders with errors
7. test_journal_entry_create_sends_idempotency_key — API call received X-Idempotency-Key header

Key differences from invoice/credit-note tests:
- No contact_id (JE has no customer)
- Lines use debit + credit (not quantity/unit_price)
- API balance check returns a plain string in detail (not a list of field errors)
- Two starter lines on the blank form (not one)
- Form field "narration" maps to entry-level description text
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

_JE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ACCOUNT_ID_1 = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID_2 = "22222222-2222-2222-2222-222222222222"

_MOCK_ACCOUNT_1 = {
    "id": _ACCOUNT_ID_1,
    "name": "Cash at Bank",
    "code": "1100",
    "account_type": "ASSET",
}
_MOCK_ACCOUNT_2 = {
    "id": _ACCOUNT_ID_2,
    "name": "Revenue",
    "code": "4000",
    "account_type": "INCOME",
}
_MOCK_ACCOUNTS = {
    "items": [_MOCK_ACCOUNT_1, _MOCK_ACCOUNT_2],
    "total": 2,
    "limit": 200,
    "offset": 0,
}

_MOCK_JE = {
    "id": _JE_ID,
    "company_id": "55555555-5555-5555-5555-555555555555",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000001",
    "entry_date": "2026-04-23",
    "description": "Accrual for April consulting",
    "status": "DRAFT",
    "reference": None,
    "posted_at": None,
    "posted_by": None,
    "reversal_of_id": None,
    "override_reason": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "66666666-6666-6666-6666-666666666666",
            "line_no": 1,
            "account_id": _ACCOUNT_ID_1,
            "description": "Cash receipt",
            "debit": "500.00",
            "credit": "0.00",
            "tax_code_id": None,
            "gst_amount": None,
            "project_id": None,
        },
        {
            "id": "77777777-7777-7777-7777-777777777777",
            "line_no": 2,
            "account_id": _ACCOUNT_ID_2,
            "description": "Revenue recognition",
            "debit": "0.00",
            "credit": "500.00",
            "tax_code_id": None,
            "gst_amount": None,
            "project_id": None,
        },
    ],
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_accounts(respx_mock: respx.MockRouter) -> None:
    """Register mock response for the accounts dropdown API call."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )


# ---------------------------------------------------------------------------
# 1. GET /journal-entries/new — requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_journal_entry_new_requires_auth() -> None:
    """GET /journal-entries/new without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/journal-entries/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. GET /journal-entries/new — form renders with two starter lines
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /journal-entries/new returns 200 with the create form and two starter line rows."""
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-entries/new")

    assert resp.status_code == 200
    # Required header fields present
    assert 'name="entry_date"' in resp.text
    # Optional narration field
    assert 'name="narration"' in resp.text
    # Idempotency key hidden input
    assert 'name="idempotency_key"' in resp.text
    # Two starter line rows rendered (index 0 and index 1)
    assert 'name="lines[0][debit]"' in resp.text
    assert 'name="lines[0][credit]"' in resp.text
    assert 'name="lines[1][debit]"' in resp.text
    assert 'name="lines[1][credit]"' in resp.text
    # Debit + Credit column headers
    assert "Debit" in resp.text
    assert "Credit" in resp.text
    # Account dropdown options populated
    assert "1100 — Cash at Bank" in resp.text
    assert "4000 — Revenue" in resp.text


# ---------------------------------------------------------------------------
# 3. GET /journal-entries/_add_line — HTMX partial returns a line row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_add_line_fragment(respx_mock: respx.MockRouter) -> None:
    """GET /journal-entries/_add_line?index=2 returns the line-row fragment, not a full page."""
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-entries/_add_line?index=2")

    assert resp.status_code == 200
    # Must be a fragment, not a full page.
    assert "<html" not in resp.text
    # Must contain the correct index in field names.
    assert 'name="lines[2][debit]"' in resp.text
    assert 'name="lines[2][credit]"' in resp.text
    # Debit and credit inputs present (JE-specific — no quantity/unit_price)
    assert 'name="lines[2][account_id]"' in resp.text
    # Dropdown options populated.
    assert "1100 — Cash at Bank" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /journal-entries/new — success (balanced entry) redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /journal-entries/new with a balanced entry mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(201, json=_MOCK_JE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/journal-entries/new",
            data={
                "entry_date": "2026-04-23",
                "narration": "Accrual for April consulting",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][description]": "Cash receipt",
                "lines[0][debit]": "500.00",
                "lines[0][credit]": "0",
                "lines[1][account_id]": _ACCOUNT_ID_2,
                "lines[1][description]": "Revenue recognition",
                "lines[1][debit]": "0",
                "lines[1][credit]": "500.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_JE_ID}"


# ---------------------------------------------------------------------------
# 5. POST /journal-entries/new — unbalanced entry re-renders with API error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_create_unbalanced_422(respx_mock: respx.MockRouter) -> None:
    """Unbalanced entry: upstream 422 with plain string detail -> re-render form with error."""
    # The API returns a plain string in detail for balance violations.
    _unbalanced_body = {"detail": "Debits and credits must balance"}
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(422, json=_unbalanced_body)
    )
    # Dropdown re-population after validation failure also needs mocking.
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/journal-entries/new",
            data={
                "entry_date": "2026-04-23",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][debit]": "500.00",
                "lines[0][credit]": "0",
                "lines[1][account_id]": _ACCOUNT_ID_2,
                "lines[1][debit]": "0",
                "lines[1][credit]": "200.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — entry_date field still present.
    assert 'name="entry_date"' in resp.text
    # API balance error message must appear.
    assert "Debits and credits must balance" in resp.text


# ---------------------------------------------------------------------------
# 6. POST /journal-entries/new — missing entry_date validation error re-renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST without entry_date: upstream 422 -> re-render the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "entry_date"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(422, json=_422_body)
    )
    # Dropdown re-population after validation failure also needs mocking.
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/journal-entries/new",
            data={
                "narration": "Missing date test",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][debit]": "100.00",
                "lines[0][credit]": "0",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — required field still present.
    assert 'name="entry_date"' in resp.text
    # Error message should appear.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 7. POST /journal-entries/new — X-Idempotency-Key header forwarded
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_create_sends_idempotency_key(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /journal-entries/new passes the idempotency_key field as X-Idempotency-Key header."""
    _idem_key = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("x-idempotency-key", ""))
        return Response(201, json=_MOCK_JE)

    respx_mock.post(f"{_API_BASE}/api/v1/journal_entries").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/journal-entries/new",
            data={
                "entry_date": "2026-04-23",
                "idempotency_key": _idem_key,
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][debit]": "500.00",
                "lines[0][credit]": "0",
                "lines[1][account_id]": _ACCOUNT_ID_2,
                "lines[1][debit]": "0",
                "lines[1][credit]": "500.00",
            },
        )

    assert len(captured) == 1, "Expected exactly one upstream POST call"
    assert captured[0] == _idem_key, f"Expected {_idem_key!r}, got {captured[0]!r}"
