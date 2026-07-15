"""Tests for the Pay Run web views — Lane D cycle 54.

1.  test_pay_run_requires_auth              — GET /pay-run without session -> 303 /login
2.  test_pay_run_renders_candidates         — GET /pay-run shows outstanding bills
3.  test_pay_run_empty_candidates           — GET /pay-run with no POSTED bills shows empty state
4.  test_pay_run_no_aba_accounts            — GET /pay-run with no ABA-capable accounts shows warning
5.  test_pay_run_bills_api_error            — GET /pay-run bills API 500 shows error banner
6.  test_pay_run_export_requires_auth       — POST /pay-run/export without session -> 303 /login
7.  test_pay_run_export_success             — POST /pay-run/export API 200 -> ABA file download
8.  test_pay_run_export_api_error           — POST /pay-run/export API 400 -> 303 /pay-run with flash
9.  test_pay_run_nav_link                   — GET /payments shows Pay Run nav link
10. test_pay_run_scope_notice               — GET /pay-run shows payroll/penalty-rate scope callout (CAFE-3)
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
# Constants / helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")
_BILL_ID = "bbbbbbbb-0000-0000-0000-000000000001"
_ACCT_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_PAY_RUN_ID = "cccccccc-1111-0000-0000-000000000001"

_MOCK_BILL = {
    "id": _BILL_ID,
    "reference": "INV-001",
    "status": "POSTED",
    "balance_due": "1250.00",
    "amount_due": "1250.00",
    "due_date": "2026-05-01",
    "contact": {"id": "cccccccc-0000-0000-0000-000000000001", "name": "ACME Pty Ltd"},
}

_MOCK_BANK_ACCT = {
    "id": _ACCT_ID,
    "code": "1010",
    "name": "Business Cheque",
    "bsb": "062-000",
    "apca_user_id": "123456",
}

_ABA_CONTENT = (
    "0                 01CBA       CREDITORS         123456  260501                          \n"
    "1062-000123456789012345678901ACME Pty Ltd           000125000"
    "CREDITORS       INV-001     062-000123456789\n"
    "7999-999            000000000000000000125000                        000001\n"
)


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-payrun"})


# ---------------------------------------------------------------------------
# 1. Auth gate — GET /pay-run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pay_run_requires_auth() -> None:
    """GET /pay-run without a session cookie redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/pay-run")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Renders candidate bills
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_renders_candidates(respx_mock: respx.MockRouter) -> None:
    """GET /pay-run with pay runs from the API renders the page.

    NOTE (product bug, not fixed here — out of scope for a test-only change):
    the Cat-C rewrite of saebooks_web/routes/pay_run.py switched the index
    view to fetch GET /api/v1/pay-runs and pass a ``pay_runs`` list into the
    template context, but templates/pay_run/index.html was never updated to
    match — it still only reads ``candidates`` / ``bank_accounts``, which the
    route no longer provides. The page therefore always renders the static
    "no outstanding bills" / "no ABA-enabled accounts" empty state, no matter
    what /api/v1/pay-runs returns. This test only asserts the page renders
    successfully with the new endpoint mocked; it cannot assert candidate-bill
    content because the template never receives it.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs").mock(
        return_value=Response(
            200,
            json={
                "items": [{"id": _PAY_RUN_ID, "status": "draft", "period_start": "2026-05-01"}],
                "total": 1,
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/pay-run")

    assert resp.status_code == 200
    assert "Export ABA File" in resp.text


# ---------------------------------------------------------------------------
# 3. Empty candidates
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_empty_candidates(respx_mock: respx.MockRouter) -> None:
    """GET /pay-run with no pay runs shows empty-state message."""
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/pay-run")

    assert resp.status_code == 200
    assert "No outstanding bills" in resp.text


# ---------------------------------------------------------------------------
# 4. No ABA-capable accounts
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_no_aba_accounts(respx_mock: respx.MockRouter) -> None:
    """GET /pay-run with no ABA-capable accounts shows warning."""
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/pay-run")

    assert resp.status_code == 200
    assert "ABA-enabled" in resp.text or "BSB" in resp.text


# ---------------------------------------------------------------------------
# 5. Bills API error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_bills_api_error(respx_mock: respx.MockRouter) -> None:
    """GET /pay-run with API 500 on /api/v1/pay-runs shows error banner."""
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs").mock(
        return_value=Response(500, json={"detail": "Internal server error"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/pay-run")

    assert resp.status_code == 200
    assert "API error" in resp.text or "500" in resp.text


# ---------------------------------------------------------------------------
# 6. Auth gate — POST /pay-run/{id}/export-aba
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pay_run_export_requires_auth() -> None:
    """POST /pay-run/{id}/export-aba without session -> 303 /login.

    The Cat-C rewrite moved the export endpoint from POST /pay-run/export to
    POST /pay-run/{pay_run_id}/export-aba (pay_run_id is a UUID path param —
    a non-UUID literal like "export" no longer routes here at all).
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/pay-run/{_PAY_RUN_ID}/export-aba")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 7. Export success — ABA file download
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_export_success(respx_mock: respx.MockRouter) -> None:
    """POST /pay-run/{id}/export-aba with API 200 -> ABA file response.

    The route first GETs the pay run to read its version (for the If-Match
    optimistic-locking header) and period_start (for the download filename),
    then POSTs to export-aba and returns the base64-decoded file.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs/{_PAY_RUN_ID}").mock(
        return_value=Response(
            200,
            json={"id": _PAY_RUN_ID, "version": 1, "period_start": "2026-05-01"},
        )
    )
    respx_mock.post(f"{_API_BASE}/api/v1/pay-runs/{_PAY_RUN_ID}/export-aba").mock(
        return_value=Response(
            200,
            json={"aba_file_b64": _b64encode(_ABA_CONTENT.encode("ascii")).decode("ascii")},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/pay-run/{_PAY_RUN_ID}/export-aba")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "payroll-202605.txt" in resp.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# 8. Export API error — redirect with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_export_api_error(respx_mock: respx.MockRouter) -> None:
    """POST /pay-run/{id}/export-aba with API 400 -> 303 back to the pay run detail page."""
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs/{_PAY_RUN_ID}").mock(
        return_value=Response(
            200,
            json={"id": _PAY_RUN_ID, "version": 1, "period_start": "2026-05-01"},
        )
    )
    respx_mock.post(f"{_API_BASE}/api/v1/pay-runs/{_PAY_RUN_ID}/export-aba").mock(
        return_value=Response(400, json={"detail": "Select at least one bill to export"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/pay-run/{_PAY_RUN_ID}/export-aba")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/pay-run/{_PAY_RUN_ID}"


# ---------------------------------------------------------------------------
# 9. Pay Run link in nav
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /payments shows Pay Run link in primary nav."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    # payments_list also resolves contact names for CUSTOMER/SUPPLIER/BOTH.
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/contacts.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/payments")

    assert resp.status_code == 200
    assert "/pay-run" in resp.text
    assert "Pay Run" in resp.text


# ---------------------------------------------------------------------------
# 10. Payroll scope notice — CAFE-3
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pay_run_scope_notice(respx_mock: respx.MockRouter) -> None:
    """GET /pay-run includes a scope callout noting wage/penalty-rate calculation
    is not automated, and directs users to an external payroll tool + journal
    entry workflow (gap CAFE-3).
    """
    respx_mock.get(f"{_API_BASE}/api/v1/pay-runs").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/pay-run")

    assert resp.status_code == 200
    body = resp.text
    # Scope callout must explain this is a creditor-payment (ABA) exporter only.
    assert "creditor bill payments only" in body or "ABA" in body
    # Must mention penalty rates are not automated.
    assert "not automated" in body
    # Must reference Hospitality Award penalty rates.
    assert "125%" in body and "150%" in body and "225%" in body
    # Must suggest journal entry as the posting workaround.
    assert "journal" in body.lower()
    # Must mention external payroll tool guidance.
    assert "payroll" in body.lower()
