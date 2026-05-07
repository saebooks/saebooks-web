"""Tests for the journal entry edit form — Lane D cycle 17.

Eight tests:
1. test_je_edit_requires_auth            — GET /journal-entries/{id}/edit without session -> 303 /login
2. test_je_edit_form_renders_draft       — mock DRAFT entry -> form with version + existing lines
3. test_je_edit_blocked_for_posted       — mock POSTED entry -> blocked page (422), no form
4. test_je_edit_success_redirects        — POST valid body; mock PATCH 200 -> 303 to detail
5. test_je_edit_conflict_shows_banner    — mock PATCH 409 + re-GET -> conflict banner + new version
6. test_je_edit_unbalanced_422           — mock PATCH 422 plain string -> re-render with __all__ error
7. test_je_edit_validation_error         — mock PATCH 422 structured -> re-render with field errors
8. test_je_edit_parses_lines_replacement — 3 lines submitted; assert PATCH body has lines:[...]*3

Key differences from invoice/credit-note edit tests:
- No contacts dropdown — JE has no customer; lines use raw debit/credit
- tax_codes dropdown is included (JE lines support optional tax_code_id)
- Locked statuses: POSTED + REVERSED (invoices use VOIDED; JEs use REVERSED)
- API balance check returns a plain string in detail (not a list of field errors)
- `narration` is the create/update field name; `description` is the Out field name
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
_ACCOUNT_ID_3 = "33333333-3333-3333-3333-333333333333"

_MOCK_ACCOUNT_1 = {"id": _ACCOUNT_ID_1, "name": "Cash at Bank", "code": "1100", "account_type": "ASSET"}
_MOCK_ACCOUNT_2 = {"id": _ACCOUNT_ID_2, "name": "Revenue", "code": "4000", "account_type": "INCOME"}
_MOCK_ACCOUNTS = {
    "items": [_MOCK_ACCOUNT_1, _MOCK_ACCOUNT_2],
    "total": 2,
    "limit": 200,
    "offset": 0,
}

_TC_ID_1 = "cccccccc-0000-0000-0000-000000000001"
_MOCK_TAX_CODES = {
    "items": [{"id": _TC_ID_1, "name": "GST on Income", "rate": "0.1000"}],
    "total": 1,
    "page": 1,
    "page_size": 500,
}

_MOCK_JE_DRAFT = {
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
    "version": 3,
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

_MOCK_JE_POSTED = {**_MOCK_JE_DRAFT, "status": "POSTED", "version": 4}

# A newer server version returned after a 409 conflict.
_MOCK_JE_V4 = {**_MOCK_JE_DRAFT, "version": 4, "description": "Updated by someone else"}

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


def _mock_tax_codes(respx_mock: respx.MockRouter) -> None:
    """Register mock response for the tax_codes dropdown API call."""
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )


# ---------------------------------------------------------------------------
# 1. Edit requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_je_edit_requires_auth() -> None:
    """GET /journal-entries/{id}/edit without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/journal-entries/{_JE_ID}/edit")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Edit form renders for DRAFT entry
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_form_renders_draft(respx_mock: respx.MockRouter) -> None:
    """GET /journal-entries/{id}/edit for a DRAFT entry renders the edit form.

    Checks:
    - version hidden input present with correct value
    - existing lines pre-populated (debit/credit values, account)
    - header field entry_date present
    - narration field present and pre-filled from entry.description
    """
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_DRAFT)
    )
    _mock_accounts(respx_mock)
    _mock_tax_codes(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/journal-entries/{_JE_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input present with correct value.
    assert 'name="version"' in resp.text
    assert 'value="3"' in resp.text
    # Idempotency key input present.
    assert 'name="idempotency_key"' in resp.text
    # Header fields present.
    assert 'name="entry_date"' in resp.text
    assert "2026-04-23" in resp.text
    # Narration pre-filled from entry.description.
    assert 'name="narration"' in resp.text
    assert "Accrual for April consulting" in resp.text
    # Existing lines present in form (debit/credit inputs for index 0 and 1).
    assert 'name="lines[0][debit]"' in resp.text
    assert 'name="lines[1][credit]"' in resp.text
    # Line descriptions pre-filled.
    assert "Cash receipt" in resp.text
    assert "Revenue recognition" in resp.text
    # Accounts dropdown populated.
    assert "1100 — Cash at Bank" in resp.text


# ---------------------------------------------------------------------------
# 3. Edit blocked for POSTED entry
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_blocked_for_posted(respx_mock: respx.MockRouter) -> None:
    """GET /journal-entries/{id}/edit for a POSTED entry shows the blocked page."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/journal-entries/{_JE_ID}/edit")

    assert resp.status_code == 422
    # Must NOT render the edit form.
    assert 'name="version"' not in resp.text
    assert 'name="entry_date"' not in resp.text
    # Must show the blocked message.
    assert "cannot be edited" in resp.text


# ---------------------------------------------------------------------------
# 4. Edit success redirects (POST happy path)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /journal-entries/{id}/edit with valid balanced body; API 200 -> 303 to detail."""
    respx_mock.patch(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_DRAFT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/edit",
            data={
                "entry_date": "2026-04-23",
                "narration": "Accrual for April consulting",
                "version": "3",
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
# 5. Edit conflict shows banner + refreshed version
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_conflict_shows_banner(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> re-render form with conflict banner + new version."""
    respx_mock.patch(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # The route re-fetches the entry after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE_V4)
    )
    _mock_accounts(respx_mock)
    _mock_tax_codes(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/edit",
            data={
                "entry_date": "2026-04-23",
                "narration": "My updated narration",
                "version": "3",  # stale
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
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

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else updated this journal entry" in resp.text
    # Hidden version input updated to the server's latest version (4).
    assert 'value="4"' in resp.text
    # User's submitted narration preserved.
    assert "My updated narration" in resp.text


# ---------------------------------------------------------------------------
# 6. POST 422 unbalanced (plain-string detail) re-renders with __all__ error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_unbalanced_422(respx_mock: respx.MockRouter) -> None:
    """PATCH returns 422 with plain-string detail (unbalanced) -> form re-renders with banner."""
    _unbalanced_body = {"detail": "Debits and credits must balance"}
    respx_mock.patch(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(422, json=_unbalanced_body)
    )
    _mock_accounts(respx_mock)
    _mock_tax_codes(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/edit",
            data={
                "entry_date": "2026-04-23",
                "version": "3",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][debit]": "500.00",
                "lines[0][credit]": "0",
                "lines[1][account_id]": _ACCOUNT_ID_2,
                "lines[1][debit]": "0",
                "lines[1][credit]": "200.00",  # unbalanced intentionally
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — entry_date field still present.
    assert 'name="entry_date"' in resp.text
    # Balance error visible.
    assert "Debits and credits must balance" in resp.text


# ---------------------------------------------------------------------------
# 7. POST 422 structured validation error re-renders with field error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    """PATCH returns 422 with structured field error -> form re-renders with error message."""
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
    respx_mock.patch(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_accounts(respx_mock)
    _mock_tax_codes(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/edit",
            data={
                "narration": "Missing date test",
                "version": "3",
                "idempotency_key": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][debit]": "100.00",
                "lines[0][credit]": "0",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — narration field still present.
    assert 'name="narration"' in resp.text
    # Field error visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 8. Existing lines preserved on re-render with errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_je_edit_parses_lines_replacement(respx_mock: respx.MockRouter) -> None:
    """POST with 3 lines; assert the PATCH body has lines:[...]*3 with correct debit/credit values."""
    captured_bodies: list[bytes] = []

    def _capture(request: respx.Request) -> Response:
        captured_bodies.append(request.content)
        return Response(200, json=_MOCK_JE_DRAFT)

    respx_mock.patch(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            f"/journal-entries/{_JE_ID}/edit",
            data={
                "entry_date": "2026-04-23",
                "narration": "Three-line entry",
                "version": "3",
                "idempotency_key": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                # Line 0 — debit
                "lines[0][account_id]": _ACCOUNT_ID_1,
                "lines[0][description]": "Cash in",
                "lines[0][debit]": "300.00",
                "lines[0][credit]": "0",
                # Line 1 — debit
                "lines[1][account_id]": _ACCOUNT_ID_2,
                "lines[1][description]": "Receivable",
                "lines[1][debit]": "200.00",
                "lines[1][credit]": "0",
                # Line 2 — credit
                "lines[2][account_id]": _ACCOUNT_ID_3,
                "lines[2][description]": "Revenue",
                "lines[2][debit]": "0",
                "lines[2][credit]": "500.00",
            },
        )

    assert len(captured_bodies) == 1, "Expected exactly one upstream PATCH call"
    body = _json.loads(captured_bodies[0])
    lines = body.get("lines", [])
    assert len(lines) == 3, f"Expected 3 lines in PATCH body, got {len(lines)}: {lines}"
    assert lines[0]["description"] == "Cash in"
    assert lines[1]["description"] == "Receivable"
    assert lines[2]["description"] == "Revenue"
    assert lines[0]["debit"] == "300.00"
    assert lines[1]["debit"] == "200.00"
    assert lines[2]["credit"] == "500.00"
