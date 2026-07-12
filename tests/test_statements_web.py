"""Tests for supplier statement reconciliation views — Gitea #28, Phase 1-4.

Route map tested:
Phase 1/2 (existing):
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

Phase 3 — Part A (queue default filter):
12. test_statements_queue_default_excludes_reconciled   — default view hides reconciled
13. test_statements_queue_default_excludes_dismissed    — default view hides dismissed
14. test_statements_queue_default_shows_needs_review    — default view shows needs_review
15. test_statements_queue_default_shows_extracted       — default view shows extracted
16. test_statements_queue_all_tab_shows_everything      — ?status=all shows all statuses
17. test_statements_queue_reconciled_tab                — ?status=reconciled passes through to API
18. test_statements_queue_dismissed_tab                 — ?status=dismissed passes through to API

Phase 3 — Part B (detail actions):
19. test_draft_missing_bill_requires_auth      — POST without session → 303 /login
20. test_draft_missing_bill_calls_api_redirects_to_bills — success → 303 to /bills/{bill_id}
21. test_draft_missing_bill_error_flash        — API error → flash on detail
22. test_dismiss_requires_auth                 — POST without session → 303 /login
23. test_dismiss_calls_api_redirects_to_queue  — success → 303 to /statements
24. test_dismiss_error_flash                   — API error → flash on detail
25. test_confirm_requires_auth                 — POST without session → 303 /login
26. test_confirm_calls_api_redirects_to_detail — success → 303 to /statements/{id}
27. test_confirm_error_flash                   — API error → flash on detail
28. test_detail_shows_draft_bill_button        — detail renders "Draft bill" for missing_in_books lines
29. test_detail_shows_dismiss_and_confirm_buttons — detail renders Dismiss + Mark reviewed buttons

Phase 4 — P4a (recon history per supplier):
30. test_detail_shows_sibling_statements       — detail fetches & renders sibling statements when contact_id present
31. test_detail_no_sibling_section_without_contact_id — no sibling section when contact_id is None

Phase 4 — P4b (add-template from detail):
32. test_add_template_requires_auth            — POST /statements/{id}/template without session → 303 /login
33. test_add_template_calls_api_and_redirects  — success → calls POST /api/v1/statement-templates, flash, redirect
34. test_add_template_missing_hint_flash       — empty prompt_hint → flash, no API call
35. test_add_template_api_error_flash          — API error → flash on detail

Phase 4 — P4c (templates list page):
36. test_templates_list_requires_auth          — GET /statement-templates without session → 303 /login
37. test_templates_list_renders                — GET /statement-templates renders template rows
38. test_templates_list_empty                  — GET /statement-templates with no items shows empty state
39. test_templates_delete_requires_auth        — POST /statement-templates/{id}/delete without session → 303 /login
40. test_templates_delete_calls_api_and_redirects — success → calls DELETE, redirects to /statement-templates
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
_LINE_ID_MISSING = "line-0001"
_CONTACT_ID = "cccccccc-1111-2222-3333-444444444444"
_SIBLING_STMT_ID = "ffffffff-1111-2222-3333-444444444444"
_TMPL_ID = "tttttttt-1111-2222-3333-444444444444"

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

# Variants for the multi-status filter tests
_MOCK_STMT_NEEDS_REVIEW = dict(_MOCK_STMT_SUMMARY, status="needs_review")
_MOCK_STMT_EXTRACTED = dict(_MOCK_STMT_SUMMARY, status="extracted")
_MOCK_STMT_RECONCILED = dict(_MOCK_STMT_SUMMARY, status="reconciled")
_MOCK_STMT_DISMISSED = dict(_MOCK_STMT_SUMMARY, status="dismissed")

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
    "status": "needs_review",
    "our_ap_as_at": 11245.67,
    "balance_delta": 1100.00,
    "contact_id": None,
    "source_document_id": 42,
    "extraction_meta": None,
    "lines": [
        {
            "id": _LINE_ID_MISSING,
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

# Detail fixture WITH a contact_id (for sibling + template tests)
_MOCK_STMT_DETAIL_WITH_CONTACT = dict(
    _MOCK_STMT_DETAIL,
    contact_id=_CONTACT_ID,
)

# A sibling statement (different id, same contact_id)
_MOCK_SIBLING_STMT = {
    "id": _SIBLING_STMT_ID,
    "supplier_name": "Acme Supplies Pty Ltd",
    "statement_date": "2026-04-30",
    "status": "reconciled",
    "closing_balance": 9800.00,
    "our_ap_as_at": 9800.00,
    "balance_delta": 0.00,
    "contact_id": _CONTACT_ID,
    "exception_count": 0,
}

# An extraction template
_MOCK_TEMPLATE = {
    "id": _TMPL_ID,
    "contact_id": _CONTACT_ID,
    "supplier_abn": "12 345 678 901",
    "supplier_name": "Acme Supplies Pty Ltd",
    "prompt_hint": "Lines start after the 'Transaction Detail' header.",
    "page_scope": "1",
    "active": True,
    "created_at": "2026-06-01T10:00:00",
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
        return_value=Response(200, json={"items": [_MOCK_STMT_NEEDS_REVIEW], "total": 1})
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
    assert "No statements" in resp.text


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
    # No contact_id → no sibling/template calls expected; mock templates to be safe.
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    body = resp.text

    # Header card fields. AU pixel-equivalence: pre-8ff3a95 these cells were
    # bare "%.2f" (no thousands separator) — money(..., grouping=False)
    # restores that byte-exact (critic round 3 fix).
    assert "Acme Supplies Pty Ltd" in body
    assert "12345.67" in body  # closing balance
    assert "11245.67" in body  # our AP

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
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
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
        return_value=Response(201, json={"id": _STMT_ID, "status": "needs_review"})
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


# ---------------------------------------------------------------------------
# Part A — Queue default filter (Phase 3)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_default_excludes_reconciled(
    respx_mock: respx.MockRouter,
) -> None:
    """Default queue view (no ?status) must NOT show reconciled statements."""
    reconciled = dict(_MOCK_STMT_RECONCILED, supplier_name="Reconciled Supplier")
    needs_review = dict(_MOCK_STMT_NEEDS_REVIEW, supplier_name="Needs Review Supplier")
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [reconciled, needs_review], "total": 2})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")  # no ?status=

    assert resp.status_code == 200
    body = resp.text
    assert "Reconciled Supplier" not in body, "reconciled should be filtered out of default view"
    assert "Needs Review Supplier" in body


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_default_excludes_dismissed(
    respx_mock: respx.MockRouter,
) -> None:
    """Default queue view (no ?status) must NOT show dismissed statements."""
    dismissed = dict(_MOCK_STMT_DISMISSED, supplier_name="Dismissed Supplier")
    extracted = dict(_MOCK_STMT_EXTRACTED, supplier_name="Extracted Supplier")
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [dismissed, extracted], "total": 2})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 200
    body = resp.text
    assert "Dismissed Supplier" not in body, "dismissed should be filtered out of default view"
    assert "Extracted Supplier" in body


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_default_shows_needs_review(
    respx_mock: respx.MockRouter,
) -> None:
    """Default queue view shows needs_review statements."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [_MOCK_STMT_NEEDS_REVIEW], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 200
    assert "Acme Supplies Pty Ltd" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_default_shows_extracted(
    respx_mock: respx.MockRouter,
) -> None:
    """Default queue view shows extracted statements."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [_MOCK_STMT_EXTRACTED], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements")

    assert resp.status_code == 200
    assert "Acme Supplies Pty Ltd" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_all_tab_shows_everything(
    respx_mock: respx.MockRouter,
) -> None:
    """?status=all tab shows all statements regardless of status."""
    reconciled = dict(_MOCK_STMT_RECONCILED, supplier_name="Reconciled Supplier")
    dismissed = dict(_MOCK_STMT_DISMISSED, supplier_name="Dismissed Supplier")
    needs_review = dict(_MOCK_STMT_NEEDS_REVIEW, supplier_name="Needs Review Supplier")
    all_items = [reconciled, dismissed, needs_review]
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": all_items, "total": 3})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements?status=all")

    assert resp.status_code == 200
    body = resp.text
    assert "Reconciled Supplier" in body
    assert "Dismissed Supplier" in body
    assert "Needs Review Supplier" in body


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_reconciled_tab(respx_mock: respx.MockRouter) -> None:
    """?status=reconciled passes the status param to the API."""
    api_route = respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [_MOCK_STMT_RECONCILED], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements?status=reconciled")

    assert resp.status_code == 200
    # Verify the API was called with status=reconciled
    assert api_route.called
    request_url = str(api_route.calls.last.request.url)
    assert "status=reconciled" in request_url


@pytest.mark.anyio
@respx.mock
async def test_statements_queue_dismissed_tab(respx_mock: respx.MockRouter) -> None:
    """?status=dismissed passes the status param to the API."""
    api_route = respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statements?status=dismissed")

    assert resp.status_code == 200
    assert api_route.called
    request_url = str(api_route.calls.last.request.url)
    assert "status=dismissed" in request_url


# ---------------------------------------------------------------------------
# Part B — Detail actions (Phase 3)
# ---------------------------------------------------------------------------


# --- Draft missing bill ---


@pytest.mark.anyio
async def test_draft_missing_bill_requires_auth() -> None:
    """POST /statements/{id}/draft-missing-bill without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/draft-missing-bill",
            data={"line_id": _LINE_ID_MISSING},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_draft_missing_bill_calls_api_redirects_to_bills(
    respx_mock: respx.MockRouter,
) -> None:
    """POST draft-missing-bill success → calls the API with line_id, redirects to /bills/{bill_id}."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/statements/{_STMT_ID}/draft-missing-bill"
    ).mock(
        return_value=Response(
            201, json={"bill_id": _BILL_ID, "statement": {"id": _STMT_ID}}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/draft-missing-bill",
            data={"line_id": _LINE_ID_MISSING},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"

    # Verify API body
    sent = _json.loads(respx_mock.calls.last.request.content)
    assert sent == {"line_id": _LINE_ID_MISSING}


@pytest.mark.anyio
@respx.mock
async def test_draft_missing_bill_error_flash(respx_mock: respx.MockRouter) -> None:
    """POST draft-missing-bill with API 422 → flash on detail page (redirect back)."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/statements/{_STMT_ID}/draft-missing-bill"
    ).mock(
        return_value=Response(422, json={"detail": "Line already billed"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/draft-missing-bill",
            data={"line_id": _LINE_ID_MISSING},
        )

    assert resp.status_code == 200
    assert "Line already billed" in resp.text or "Draft bill failed" in resp.text


# --- Dismiss ---


@pytest.mark.anyio
async def test_dismiss_requires_auth() -> None:
    """POST /statements/{id}/dismiss without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/statements/{_STMT_ID}/dismiss")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_dismiss_calls_api_redirects_to_queue(
    respx_mock: respx.MockRouter,
) -> None:
    """POST dismiss success → calls the dismiss API, redirects to /statements."""
    respx_mock.post(f"{_API_BASE}/api/v1/statements/{_STMT_ID}/dismiss").mock(
        return_value=Response(200, json={"id": _STMT_ID, "status": "dismissed"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/statements/{_STMT_ID}/dismiss")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/statements"


@pytest.mark.anyio
@respx.mock
async def test_dismiss_error_flash(respx_mock: respx.MockRouter) -> None:
    """POST dismiss with API error → flash on detail page (redirect back)."""
    respx_mock.post(f"{_API_BASE}/api/v1/statements/{_STMT_ID}/dismiss").mock(
        return_value=Response(422, json={"detail": "Cannot dismiss reconciled statement"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(f"/statements/{_STMT_ID}/dismiss")

    assert resp.status_code == 200
    assert "Cannot dismiss" in resp.text or "Dismiss failed" in resp.text


# --- Confirm ---


@pytest.mark.anyio
async def test_confirm_requires_auth() -> None:
    """POST /statements/{id}/confirm without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/statements/{_STMT_ID}/confirm")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_confirm_calls_api_redirects_to_detail(
    respx_mock: respx.MockRouter,
) -> None:
    """POST confirm success → calls the confirm API, redirects back to /statements/{id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/statements/{_STMT_ID}/confirm").mock(
        return_value=Response(200, json={"id": _STMT_ID, "status": "reconciled"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/statements/{_STMT_ID}/confirm")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/statements/{_STMT_ID}"


@pytest.mark.anyio
@respx.mock
async def test_confirm_error_flash(respx_mock: respx.MockRouter) -> None:
    """POST confirm with API error → flash on detail page (redirect back)."""
    respx_mock.post(f"{_API_BASE}/api/v1/statements/{_STMT_ID}/confirm").mock(
        return_value=Response(422, json={"detail": "Statement has unresolved exceptions"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(f"/statements/{_STMT_ID}/confirm")

    assert resp.status_code == 200
    assert "Statement has unresolved" in resp.text or "Confirm failed" in resp.text


# --- Detail button rendering ---


@pytest.mark.anyio
@respx.mock
async def test_detail_shows_draft_bill_button(respx_mock: respx.MockRouter) -> None:
    """Detail page renders a 'Draft bill' button for missing_in_books lines."""
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    body = resp.text
    # Draft bill button form present
    assert "draft-missing-bill" in body
    assert f'name="line_id" value="{_LINE_ID_MISSING}"' in body
    assert "Draft bill" in body


@pytest.mark.anyio
@respx.mock
async def test_detail_shows_dismiss_and_confirm_buttons(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page renders Dismiss and Mark reviewed buttons for an actionable statement."""
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    body = resp.text
    assert f"/statements/{_STMT_ID}/dismiss" in body
    assert f"/statements/{_STMT_ID}/confirm" in body
    assert "Mark reviewed" in body
    assert "Dismiss" in body


# ---------------------------------------------------------------------------
# Phase 4 — P4a: Recon history per supplier
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_detail_shows_sibling_statements(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page fetches sibling statements when contact_id is present and renders them."""
    # Statement with a contact_id
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL_WITH_CONTACT))
    )
    # Sibling list (includes current + one sibling; route excludes current)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements\?.*contact_id.*$").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    dict(_MOCK_STMT_DETAIL_WITH_CONTACT, lines=[]),  # current — excluded by route
                    _MOCK_SIBLING_STMT,
                ],
                "total": 2,
            },
        )
    )
    # Templates call (no templates for this test)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    body = resp.text

    # Sibling section heading
    assert "Other statements from this supplier" in body
    # Sibling row data
    assert "2026-04-30" in body       # sibling statement_date
    assert _SIBLING_STMT_ID in body   # link to sibling
    assert "Reconciled" in body       # sibling status badge
    # Current statement must NOT appear in the sibling list
    assert body.count(_STMT_ID) >= 1  # present at least once (in own header)


@pytest.mark.anyio
@respx.mock
async def test_detail_no_sibling_section_without_contact_id(
    respx_mock: respx.MockRouter,
) -> None:
    """When contact_id is None the sibling section must not appear and no sibling API call is made."""
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL))  # contact_id: None
    )
    # No statement-templates call expected either since no contact_id
    sibling_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/statements\?.*contact_id.*$"
    ).mock(return_value=Response(200, json={"items": [], "total": 0}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/statements/{_STMT_ID}")

    assert resp.status_code == 200
    assert "Other statements from this supplier" not in resp.text
    # The sibling-list endpoint must NOT have been called
    assert not sibling_route.called


# ---------------------------------------------------------------------------
# Phase 4 — P4b: Add extraction template from detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_add_template_requires_auth() -> None:
    """POST /statements/{id}/template without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/template",
            data={"prompt_hint": "Some hint"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_add_template_calls_api_and_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /statements/{id}/template → fetches statement, calls POST /api/v1/statement-templates,
    flashes success message, and redirects to the detail."""
    # The route first fetches the statement for contact_id / abn / name
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL_WITH_CONTACT))
    )
    template_route = respx_mock.post(f"{_API_BASE}/api/v1/statement-templates").mock(
        return_value=Response(201, json={"id": _TMPL_ID})
    )
    # Detail page mock for the follow-redirect path
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": [_MOCK_TEMPLATE]})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements\?.*contact_id.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/template",
            data={"prompt_hint": "Lines start after header.", "page_scope": "1"},
        )

    assert resp.status_code == 200
    assert "Template saved" in resp.text or "re-ingest" in resp.text

    # Verify the template-create API was called
    assert template_route.called
    sent = _json.loads(template_route.calls.last.request.content)
    assert sent["prompt_hint"] == "Lines start after header."
    assert sent["page_scope"] == "1"
    assert sent["contact_id"] == _CONTACT_ID


@pytest.mark.anyio
@respx.mock
async def test_add_template_missing_hint_flash(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /statements/{id}/template with empty prompt_hint → flash validation error,
    no API calls to statement-templates."""
    template_route = respx_mock.post(f"{_API_BASE}/api/v1/statement-templates").mock(
        return_value=Response(201, json={"id": _TMPL_ID})
    )
    # Detail page mocks for redirect
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL_WITH_CONTACT))
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements\?.*contact_id.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/template",
            data={"prompt_hint": ""},  # empty
        )

    assert resp.status_code == 200
    assert "Prompt hint is required" in resp.text or "required" in resp.text.lower()
    assert not template_route.called


@pytest.mark.anyio
@respx.mock
async def test_add_template_api_error_flash(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /statements/{id}/template with API 422 → flash error, redirects to detail."""
    respx_mock.get(f"{_API_BASE}/api/v1/statements/{_STMT_ID}").mock(
        return_value=Response(200, json=dict(_MOCK_STMT_DETAIL_WITH_CONTACT))
    )
    respx_mock.post(f"{_API_BASE}/api/v1/statement-templates").mock(
        return_value=Response(422, json={"detail": "Duplicate template for supplier"})
    )
    # Detail page mocks for redirect
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statement-templates.*$").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/statements\?.*contact_id.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            f"/statements/{_STMT_ID}/template",
            data={"prompt_hint": "Some hint"},
        )

    assert resp.status_code == 200
    assert "Duplicate template" in resp.text or "Template save failed" in resp.text


# ---------------------------------------------------------------------------
# Phase 4 — P4c: Templates list page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_templates_list_requires_auth() -> None:
    """GET /statement-templates without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/statement-templates")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_templates_list_renders(respx_mock: respx.MockRouter) -> None:
    """GET /statement-templates renders template rows."""
    respx_mock.get(f"{_API_BASE}/api/v1/statement-templates").mock(
        return_value=Response(200, json={"items": [_MOCK_TEMPLATE]})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statement-templates")

    assert resp.status_code == 200
    body = resp.text
    assert "Extraction Templates" in body
    assert "Acme Supplies Pty Ltd" in body
    assert "Lines start after the" in body  # prompt_hint
    assert "1" in body                       # page_scope
    # Delete button form present
    assert f"/statement-templates/{_TMPL_ID}/delete" in body


@pytest.mark.anyio
@respx.mock
async def test_templates_list_empty(respx_mock: respx.MockRouter) -> None:
    """GET /statement-templates with no items shows empty state."""
    respx_mock.get(f"{_API_BASE}/api/v1/statement-templates").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/statement-templates")

    assert resp.status_code == 200
    assert "No extraction templates" in resp.text


@pytest.mark.anyio
async def test_templates_delete_requires_auth() -> None:
    """POST /statement-templates/{id}/delete without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/statement-templates/{_TMPL_ID}/delete")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_templates_delete_calls_api_and_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /statement-templates/{id}/delete → calls DELETE /api/v1/statement-templates/{id},
    then redirects to /statement-templates."""
    delete_route = respx_mock.delete(
        f"{_API_BASE}/api/v1/statement-templates/{_TMPL_ID}"
    ).mock(return_value=Response(204))
    # Mock the list page for follow-redirect
    respx_mock.get(f"{_API_BASE}/api/v1/statement-templates").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(f"/statement-templates/{_TMPL_ID}/delete")

    assert resp.status_code == 200
    assert delete_route.called
    assert "Template deleted" in resp.text or "No extraction templates" in resp.text
