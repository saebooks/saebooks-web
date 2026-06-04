"""Tests for supplier statement reconciliation views — Gitea #28, Phase 1.

Route map tested:
1.  test_statements_queue_requires_auth        — 303 → /login without session
2.  test_statements_queue_renders              — renders statements table
3.  test_statements_queue_empty                — empty state message shown
4.  test_statements_queue_api_error            — API error banner shown
5.  test_statements_detail_requires_auth       — 303 → /login without session
6.  test_statements_detail_renders             — detail page renders header card + lines
7.  test_statements_detail_lines_status_colours — match_status drives row styling
8.  test_statements_ingest_requires_auth       — POST without session → 303 /login
9.  test_statements_ingest_calls_api_and_redirects — success → 303 to detail
10. test_statements_ingest_error_flash         — API error → 303 to queue with flash
11. test_statements_ingest_invalid_id          — non-numeric doc id → flash, no API call
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
# Constants / fixtures
# ---------------------------------------------------------------------------

_STMT_ID = "dddddddd-1111-2222-3333-444444444444"
_BILL_ID = "eeeeeeee-1111-2222-3333-444444444444"

_MOCK_STMT_SUMMARY = {
    "id": _STMT_ID,
    "supplier_name": "Acme Supplies Pty Ltd",
    "statement_date": "2026-05-31",
    "status": "pending",
    "closing_balance": 12345.67,
    "our_ap_as_at": 11245.67,
    "balance_delta": 1100.00,
    "source_document_id": 42,
    "exception_count": 2,
}

_MOCK_STMT_DETAIL = {
    "id": _STMT_ID,
    "supplier_name": "Acme Supplies Pty Ltd",
    "supplier_abn": "12 345 678 901",
    "customer_ref": "CUST-001",
    "statement_date": "2026-05-31",
    "terms": "Net 30",
    "opening_balance": 5000.00,
    "closing_balance": 12345.67,
    "currency": "AUD",
    "status": "pending",
    "our_ap_as_at": 11245.67,
    "balance_delta": 1100.00,
    "contact_id": None,
    "source_document_id": 42,
    "extraction_meta": None,
    "lines": [
        {
            "id": "line-0001",
            "line_date": "2026-05-01",
            "line_type": "INVOICE",
            "reference": "INV-9999",
            "description": "Widget supply",
            "amount": 1100.00,
            "match_status": "missing_in_books",
            "matched_bill_id": None,
            "note": "on statement, not in our books",
        },
        {
            "id": "line-0002",
            "line_date": "2026-05-10",
            "line_type": "INVOICE",
            "reference": "INV-1002",
            "description": "Consulting",
            "amount": 550.00,
            "match_status": "amount_mismatch",
            "matched_bill_id": None,
            "note": "statement 550.00 vs books 545.00",
        },
        {
            "id": "line-0003",
            "line_date": None,
            "line_type": "INVOICE",
            "reference": "INV-8001",
            "description": "Old invoice",
            "amount": 200.00,
            "match_status": "not_on_statement",
            "matched_bill_id": _BILL_ID,
            "note": "in books, not on statement",
        },
        {
            "id": "line-0004",
            "line_date": "2026-05-01",
            "line_type": "INVOICE",
            "reference": "INV-1001",
            "description": "Steel supply",
            "amount": 4500.00,
            "match_status": "matched",
            "matched_bill_id": _BILL_ID,
            "note": "",
        },
        {
            "id": "line-0005",
            "line_date": "2026-05-20",
            "line_type": "PAYMENT",
            "reference": None,
            "description": "Payment received",
            "amount": -2000.00,
            "match_status": "payment_info",
            "matched_bill_id": None,
            "note": "payment shown on statement",
        },
        {
            "id": "line-0006",
            "line_date": "2026-04-01",
            "line_type": "INVOICE",
            "reference": "INV-7000",
            "description": "Old settled invoice",
            "amount": 300.00,
            "match_status": "settled_not_in_books",
            "matched_bill_id": None,
            "note": "",
        },
    ],
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-stmts"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Queue: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_statements_queue_requires_auth() -> None:
    """GET /statements without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Queue: renders statements table
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_renders(respx_mock: respx.MockRouter) -> None:
    """GET /statements renders the queue table with statement rows."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [_MOCK_STMT_SUMMARY], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 200
    body = resp.text
    assert "Acme Supplies Pty Ltd" in body
    assert "2026-05-31" in body
    assert "1,100.00" in body or "1100.00" in body  # balance delta
    assert _STMT_ID in body  # link to detail
    assert "Ingest" in body  # ingest form present


# ---------------------------------------------------------------------------
# 3. Queue: empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_empty(respx_mock: respx.MockRouter) -> None:
    """GET /statements with no items shows the empty state message."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 200
    assert "No statements found" in resp.text


# ---------------------------------------------------------------------------
# 4. Queue: API error banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_api_error(respx_mock: respx.MockRouter) -> None:
    """GET /statements with API 500 renders the error banner."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(500, json={"detail": "Internal server error"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 200
    assert "API error" in resp.text


# ---------------------------------------------------------------------------
# 5. Detail: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_statements_detail_requires_auth() -> None:
    """GET /statements/{id} without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 6. Detail: renders header card + lines
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_detail_renders(respx_mock: respx.MockRouter) -> None:
    """GET /statements/{id} renders balance card and lines table."""
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    body = resp.text

    # Header card fields
    assert "Acme Supplies Pty Ltd" in body
    assert "12,345.67" in body or "12345.67" in body  # closing balance
    assert "11,245.67" in body or "11245.67" in body  # our AP

    # Lines table
    assert "INV-9999" in body          # missing_in_books line
    assert "Widget supply" in body
    assert "INV-1001" in body          # matched line
    assert "Steel supply" in body
    assert "Payment received" in body  # payment_info line

    # Back link
    assert "← statements" in body


# ---------------------------------------------------------------------------
# 7. Detail: lines colour-coded by match_status
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_detail_lines_status_colours(respx_mock: respx.MockRouter) -> None:
    """Detail page must render colour attributes for all six match_status values."""
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    body = resp.text

    # Each match_status value must produce its badge label
    assert "Missing in Books" in body
    assert "Amount Mismatch" in body
    assert "Not on Statement" in body
    assert "Matched" in body
    assert "Payment / Credit" in body
    assert "Settled (not in books)" in body

    # data-match-status attributes present for all six statuses
    assert 'data-match-status="missing_in_books"' in body
    assert 'data-match-status="amount_mismatch"' in body
    assert 'data-match-status="not_on_statement"' in body
    assert 'data-match-status="matched"' in body
    assert 'data-match-status="payment_info"' in body
    assert 'data-match-status="settled_not_in_books"' in body

    # Exceptions should sort before matched (missing_in_books first)
    missing_pos = body.index("missing_in_books")
    matched_pos = body.index('"matched"')
    assert missing_pos < matched_pos, (
        "missing_in_books line should appear before matched line in rendered output"
    )


# ---------------------------------------------------------------------------
# 8. Ingest: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_statements_ingest_requires_auth() -> None:
    """POST /statements/ingest without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/statements/ingest",
            data={"paperless_document_id": "42"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 9. Ingest: success → calls API, redirects to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_ingest_calls_api_and_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /statements/ingest with valid doc_id → calls /api/v1/statements/ingest,
    then redirects (303) to /statements/{new_id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/statements/ingest").mock(
        return_value=Response(201, json={"id": _STMT_ID, "status": "pending"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/statements/ingest",
            data={"paperless_document_id": "42"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/statements/{_STMT_ID}"

    # Verify the API call was made with correct body
    assert respx_mock.calls.last is not None
    sent_body = _json.loads(respx_mock.calls.last.request.content)
    assert sent_body == {"paperless_document_id": 42}


# ---------------------------------------------------------------------------
# 10. Ingest: API error → redirect to queue with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_ingest_error_flash(respx_mock: respx.MockRouter) -> None:
    """POST /statements/ingest with API 422 → 303 to /statements with flash message."""
    respx_mock.post(f"{_API_BASE}/api/v1/statements/ingest").mock(
        return_value=Response(422, json={"detail": "Document already ingested"})
    )
    # Mock the queue page (for follow_redirects=True path)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            "/statements/ingest",
            data={"paperless_document_id": "42"},
        )

    assert resp.status_code == 200
    assert "Document already ingested" in resp.text or "Ingest failed" in resp.text


# ---------------------------------------------------------------------------
# 11. Ingest: non-numeric document ID → flash, no API call
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_ingest_invalid_id(respx_mock: respx.MockRouter) -> None:
    """POST /statements/ingest with a non-numeric doc_id → redirect to /statements
    with a validation flash; the ingest API must NOT be called."""
    ingest_route = respx_mock.post(f"{_API_BASE}/api/v1/statements/ingest").mock(
        return_value=Response(201, json={"id": _STMT_ID})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            "/statements/ingest",
            data={"paperless_document_id": "not-a-number"},
        )

    assert resp.status_code == 200
    assert "Invalid document ID" in resp.text
    # API ingest must not have been called
    assert len(ingest_route.calls) == 0
