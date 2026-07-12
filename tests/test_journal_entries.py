"""Tests for the journal entries list + detail views — Lane D cycle 6.

Five tests:
1. test_journal_entries_requires_auth         — 303 → /login without session
2. test_journal_entries_list_renders_row      — full-page render contains a table row
3. test_journal_entries_list_partial_htmx     — HX-Request returns fragment (no <html>)
4. test_journal_entries_detail_renders        — detail page: debit + credit columns, totals row
5. test_journal_entries_detail_404_propagates — upstream 404 → HTTP 404 response

API shape verified from saebooks/api/v1/schemas.py JournalEntryOut / JournalLineOut:
- ref: str (e.g. JE-000001)
- entry_date: date
- description: str | None
- status: DRAFT / POSTED / REVERSED
- posted_at: datetime | None
- lines[]: line_no, account_id, description, debit (Decimal), credit (Decimal)
- No top-level total_debit/total_credit — computed in template from lines.
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
# Fixtures / helpers
# ---------------------------------------------------------------------------

_JE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ACCT_ID_1 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ACCT_ID_2 = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_MOCK_JE = {
    "id": _JE_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000001",
    "entry_date": "2026-04-15",
    "description": "Accrual for April consulting",
    "status": "POSTED",
    "reference": None,
    "posted_at": "2026-04-15T09:00:00Z",
    "posted_by": "api:testuser",
    "reversal_of_id": None,
    "override_reason": None,
    "version": 1,
    "created_at": "2026-04-15T08:00:00Z",
    "updated_at": "2026-04-15T09:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "line_no": 1,
            "account_id": _ACCT_ID_1,
            "description": "Consulting revenue accrual — debit",
            "debit": "1000.00",
            "credit": "0.00",
            "tax_code_id": None,
            "gst_amount": None,
            "project_id": None,
        },
        {
            "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "line_no": 2,
            "account_id": _ACCT_ID_2,
            "description": "Consulting revenue accrual — credit",
            "debit": "0.00",
            "credit": "1000.00",
            "tax_code_id": None,
            "gst_amount": None,
            "project_id": None,
        },
    ],
}

_MOCK_JES_RESPONSE = {
    "items": [_MOCK_JE],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_journal_entries_requires_auth() -> None:
    """GET /journal-entries without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/journal-entries")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_journal_entries_list_renders_row(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /journal-entries renders the JE ref in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(200, json=_MOCK_JES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-entries")

    assert resp.status_code == 200
    # Full page — must contain the outer HTML scaffold.
    assert "<html" in resp.text
    # JE ref should appear.
    assert "JE-000001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_journal_entries_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    """GET /journal-entries with HX-Request header returns the fragment, not a full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(200, json=_MOCK_JES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/journal-entries",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    # Fragment must NOT contain the full <html> wrapper.
    assert "<html" not in resp.text
    # But it should still contain the JE data.
    assert "JE-000001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_journal_entries_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /journal-entries/{id} renders the debit/credit lines table and totals row.

    Asserts:
    - JE ref appears in the page.
    - "Debit" and "Credit" column headings are present.
    - Line description text appears.
    - Totals row with summed debit and credit values is rendered.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(200, json=_MOCK_JE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/journal-entries/{_JE_ID}")

    assert resp.status_code == 200
    assert "JE-000001" in resp.text
    # Debit and credit column headings must appear.
    assert "Debit" in resp.text
    assert "Credit" in resp.text
    # Line descriptions.
    assert "Consulting revenue accrual" in resp.text
    # Totals row — both sides should show 1000.00.
    assert "1,000.00" in resp.text
    # Totals label.
    assert "Totals" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_journal_entries_detail_404_propagates(respx_mock: respx.MockRouter) -> None:
    """When the upstream API returns 404, the detail view returns HTTP 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}").mock(
        return_value=Response(404, json={"detail": "Journal entry not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/journal-entries/{_JE_ID}")

    assert resp.status_code == 404
