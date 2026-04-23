"""Tests for the bank statement lines list + detail views — Lane D cycle 27.

Four tests:
1. test_bank_statement_lines_list_renders      — full-page GET 200 with description in body
2. test_bank_statement_lines_list_htmx_partial — HX-Request returns fragment (no <html>)
3. test_bank_statement_lines_list_status_filter — status filter is passed to upstream API
4. test_bank_statement_lines_detail_renders    — detail page shows date, amount, status
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

_LINE_ID = "cccccccc-cccc-cccc-cccc-000000000002"
_ACCOUNT_ID = "dddddddd-dddd-dddd-dddd-000000000001"

_MOCK_LINE = {
    "id": _LINE_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "account_id": _ACCOUNT_ID,
    "txn_date": "2026-04-15",
    "description": "PAYROLL TRANSFER",
    "amount": "-4500.00",
    "balance": "12000.00",
    "reference": "PAY-2026-04",
    "status": "UNMATCHED",
    "matched_entry_id": None,
    "matched_at": None,
    "matched_by": None,
    "contact_id": None,
    "bank_rule_id": None,
    "bank_feed_account_id": None,
    "external_id": "EXT-001",
    "version": 1,
    "created_at": "2026-04-15T10:00:00Z",
    "archived_at": None,
}

_MOCK_LINES_RESPONSE = {
    "items": [_MOCK_LINE],
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
@respx.mock
async def test_bank_statement_lines_list_renders(respx_mock: respx.MockRouter) -> None:
    """Full-page GET /bank-statement-lines renders description in the table."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines").mock(
        return_value=Response(200, json=_MOCK_LINES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-statement-lines")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "PAYROLL TRANSFER" in resp.text
    assert "2026-04-15" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_statement_lines_list_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /bank-statement-lines with HX-Request header returns fragment, not full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines").mock(
        return_value=Response(200, json=_MOCK_LINES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/bank-statement-lines",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "PAYROLL TRANSFER" in resp.text
    assert "bank-statement-lines-table" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_statement_lines_list_status_filter(respx_mock: respx.MockRouter) -> None:
    """Status filter is forwarded to the upstream API as a query param."""
    route = respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines").mock(
        return_value=Response(200, json=_MOCK_LINES_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bank-statement-lines?status=UNMATCHED")

    assert resp.status_code == 200
    # Confirm the upstream call included the status param.
    assert route.called
    called_url = str(route.calls[0].request.url)
    assert "status=UNMATCHED" in called_url


@pytest.mark.anyio
@respx.mock
async def test_bank_statement_lines_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /bank-statement-lines/{id} renders date, amount, and status."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines/{_LINE_ID}").mock(
        return_value=Response(200, json=_MOCK_LINE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bank-statement-lines/{_LINE_ID}")

    assert resp.status_code == 200
    assert "2026-04-15" in resp.text
    assert "PAYROLL TRANSFER" in resp.text
    assert "-4500" in resp.text
    assert "Unmatched" in resp.text
