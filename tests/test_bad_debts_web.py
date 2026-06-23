"""Tests for the bad-debt candidate screen + HTMX write-off action.

Phase 2 / Task 9 (screen, filter, review write-off) and Task 12 (engine
calls mocked). The web app NEVER posts the ledger entry — it proxies to the
engine ``/api/v1/invoices/{id}/write-off`` endpoint, which is mocked here.

Filter rule under test:
    candidate  ==  status==POSTED  AND  balance(total-amount_paid) > 0
                   AND  age(today - due_date) > writeoff_threshold_days
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from datetime import date, timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API_BASE = settings.api_url.rstrip("/")
_COMPANY_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_OLD_INVOICE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_RECENT_INVOICE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_PAID_INVOICE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "TEST_SESSION_TOKEN"})


def _company(mode: str = "review", threshold: int = 90) -> dict:
    return {
        "id": _COMPANY_ID,
        "name": "Acme Pty Ltd",
        "version": 3,
        "writeoff_mode": mode,
        "writeoff_threshold_days": threshold,
        "recovery_mode": "smart_prompt",
        "bad_debt_recovery_account": None,
    }


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _invoice(inv_id: str, *, due_days_ago: int, total: str, paid: str,
             status: str = "POSTED", number: str = "INV-X") -> dict:
    return {
        "id": inv_id,
        "contact_id": _CONTACT_ID,
        "number": number,
        "issue_date": _iso(due_days_ago + 30),
        "due_date": _iso(due_days_ago),
        "status": status,
        "total": total,
        "amount_paid": paid,
        "one_off_customer_name": None,
    }


_CONTACTS = {"items": [{"id": _CONTACT_ID, "name": "Slow Payer Pty"}], "total": 1}


def _mock_listing(respx_mock: respx.MockRouter, *, company: dict, invoices: list[dict]) -> None:
    """Wire the three GET endpoints the screen calls."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": invoices, "total": len(invoices)})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json=_CONTACTS)
    )


# ---------------------------------------------------------------------------
# 1. Filter logic — only the genuinely-old, unpaid, POSTED invoice shows.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_filters_candidates(respx_mock: respx.MockRouter) -> None:
    """Only POSTED + balance>0 + age>threshold lands on the screen."""
    invoices = [
        _invoice(_OLD_INVOICE_ID, due_days_ago=200, total="500.00", paid="0.00", number="INV-OLD"),
        # Recent (30d < 90d threshold) — excluded by age.
        _invoice(_RECENT_INVOICE_ID, due_days_ago=30, total="300.00", paid="0.00", number="INV-NEW"),
        # Old but fully paid — excluded by balance.
        _invoice(_PAID_INVOICE_ID, due_days_ago=200, total="400.00", paid="400.00", number="INV-PAID"),
    ]
    _mock_listing(respx_mock, company=_company("review", 90), invoices=invoices)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bad-debts")

    assert resp.status_code == 200
    assert "INV-OLD" in resp.text
    assert "INV-NEW" not in resp.text       # too recent
    assert "INV-PAID" not in resp.text      # nothing owed
    assert "Slow Payer Pty" in resp.text    # contact name resolved
    assert "1 candidate" in resp.text


# ---------------------------------------------------------------------------
# 2. Review mode renders a per-row write-off button (HTMX).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_review_mode_shows_writeoff_button(respx_mock: respx.MockRouter) -> None:
    invoices = [_invoice(_OLD_INVOICE_ID, due_days_ago=200, total="500.00", paid="0.00", number="INV-OLD")]
    _mock_listing(respx_mock, company=_company("review", 90), invoices=invoices)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bad-debts")

    assert resp.status_code == 200
    assert f'hx-post="/bad-debts/{_OLD_INVOICE_ID}/write-off"' in resp.text
    assert "Write off" in resp.text


# ---------------------------------------------------------------------------
# 3. Auto mode is read-only — no write-off button, shows the auto banner.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_auto_mode_read_only(respx_mock: respx.MockRouter) -> None:
    invoices = [_invoice(_OLD_INVOICE_ID, due_days_ago=200, total="500.00", paid="0.00", number="INV-OLD")]
    _mock_listing(respx_mock, company=_company("auto", 90), invoices=invoices)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bad-debts")

    assert resp.status_code == 200
    assert "INV-OLD" in resp.text
    assert "Auto mode" in resp.text
    assert "/write-off" not in resp.text  # no interactive button


# ---------------------------------------------------------------------------
# 4. Threshold respected — a custom threshold changes the candidate set.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_custom_threshold(respx_mock: respx.MockRouter) -> None:
    """A 30-day-overdue invoice IS a candidate when threshold is 14, not 90."""
    invoices = [_invoice(_RECENT_INVOICE_ID, due_days_ago=30, total="300.00", paid="0.00", number="INV-30D")]
    _mock_listing(respx_mock, company=_company("review", 14), invoices=invoices)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bad-debts")

    assert resp.status_code == 200
    assert "INV-30D" in resp.text  # 30 > 14 → candidate


# ---------------------------------------------------------------------------
# 5. HTMX write-off success → engine called, written-off fragment returned.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_writeoff_action_calls_engine(respx_mock: respx.MockRouter) -> None:
    calls: list[str] = []

    def _capture(request: respx.Request, *_: object) -> Response:
        calls.append(str(request.url))
        return Response(200, json={"id": _OLD_INVOICE_ID, "number": "INV-OLD", "status": "WRITTEN_OFF"})

    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_OLD_INVOICE_ID}/write-off"
    ).mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/bad-debts/{_OLD_INVOICE_ID}/write-off",
            data={"reason": "120 days no contact"},
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert calls, "engine write-off endpoint not called"
    assert "Written off" in resp.text
    # The fragment swaps the same row id back in.
    assert f'id="bad-debt-row-{_OLD_INVOICE_ID}"' in resp.text


# ---------------------------------------------------------------------------
# 6. HTMX write-off conflict (already written off) → error fragment, 409.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_writeoff_action_conflict(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_OLD_INVOICE_ID}/write-off"
    ).mock(return_value=Response(409, json={"detail": "Invoice already written off"}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/bad-debts/{_OLD_INVOICE_ID}/write-off",
            data={},
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 409
    assert "already written off" in resp.text.lower()


# ---------------------------------------------------------------------------
# 7. Empty state — no candidates renders the reassuring empty card.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bad_debts_empty_state(respx_mock: respx.MockRouter) -> None:
    _mock_listing(respx_mock, company=_company("review", 90), invoices=[])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bad-debts")

    assert resp.status_code == 200
    assert "No write-off candidates" in resp.text
