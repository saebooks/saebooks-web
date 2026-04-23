"""Tests for the dashboard home page — Lane D cycle 21.

Six tests:
1. test_dashboard_returns_200_and_title       — GET / returns 200 with "Dashboard" in HTML
2. test_dashboard_ar_tile                     — mocked AR counts/totals render
3. test_dashboard_ap_tile                     — mocked AP counts/totals render
4. test_dashboard_cash_tile                   — cash IN/OUT/net renders
5. test_dashboard_recent_activity             — recent activity shows mixed entity rows
6. test_dashboard_empty_data_no_errors        — all zeros / empty lists render without error
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")

# Today for overdue / due-soon calculations.
_TODAY = date.today().isoformat()
_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
_IN_3_DAYS = (date.today() + timedelta(days=3)).isoformat()
_MONTH_START = date.today().replace(day=1).isoformat()


def _inv(
    id_: str = "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    number: str = "INV-0001",
    status: str = "DRAFT",
    due_date: str = _TODAY,
    total: str = "100.00",
    created_at: str = "2026-04-01T01:00:00Z",
) -> dict:
    return {
        "id": id_,
        "number": number,
        "status": status,
        "issue_date": _TODAY,
        "due_date": due_date,
        "total": total,
        "currency": "AUD",
        "created_at": created_at,
        "updated_at": created_at,
    }


def _bill(
    id_: str = "bbbb0001-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    number: str = "BILL-0001",
    status: str = "DRAFT",
    due_date: str = _TODAY,
    total: str = "200.00",
    created_at: str = "2026-04-02T01:00:00Z",
) -> dict:
    return {
        "id": id_,
        "number": number,
        "status": status,
        "issue_date": _TODAY,
        "due_date": due_date,
        "total": total,
        "currency": "AUD",
        "created_at": created_at,
        "updated_at": created_at,
    }


def _payment(
    id_: str = "cccc0001-cccc-cccc-cccc-cccccccccccc",
    number: str = "PAY-0001",
    direction: str = "INCOMING",
    amount: str = "150.00",
    payment_date: str | None = None,
    created_at: str = "2026-04-03T01:00:00Z",
) -> dict:
    return {
        "id": id_,
        "number": number,
        "direction": direction,
        "amount": amount,
        "payment_date": payment_date or _TODAY,
        "currency": "AUD",
        "created_at": created_at,
        "updated_at": created_at,
    }


def _je(
    id_: str = "dddd0001-dddd-dddd-dddd-dddddddddddd",
    number: str = "JE-0001",
    created_at: str = "2026-04-04T01:00:00Z",
) -> dict:
    return {
        "id": id_,
        "number": number,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _contact(
    id_: str = "eeee0001-eeee-eeee-eeee-eeeeeeeeeeee",
    name: str = "Test Corp",
    created_at: str = "2026-04-05T01:00:00Z",
) -> dict:
    return {
        "id": id_,
        "name": name,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _page(items: list) -> dict:
    return {"items": items, "total": len(items), "page": 1, "pages": 1}


def _register_mocks(
    respx_mock: respx.MockRouter,
    *,
    draft_invoices: list | None = None,
    open_invoices: list | None = None,
    paid_invoices: list | None = None,
    draft_bills: list | None = None,
    open_bills: list | None = None,
    paid_bills: list | None = None,
    payments: list | None = None,
    recent_invoices: list | None = None,
    recent_bills: list | None = None,
    recent_payments: list | None = None,
    recent_je: list | None = None,
    recent_contacts: list | None = None,
) -> None:
    """Register all 12 API mocks that the dashboard fires in parallel.

    respx matches on URL + query params; we use pattern=... with regex=True
    to match any request to the path regardless of query-string order.
    """
    def _mock(path: str, items: list) -> None:
        respx_mock.get(url__regex=rf"^{_API_BASE}{path}(\?.*)?$").mock(
            return_value=Response(200, json=_page(items))
        )

    # The dashboard fires these 12 requests; respx routes by first match so
    # we register the most-specific (with status filter) before the open ones.

    # AR
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/invoices\?.*status=DRAFT.*$").mock(
        return_value=Response(200, json=_page(draft_invoices or []))
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/invoices\?.*status=SENT%2CPARTIALLY_PAID.*$"
    ).mock(return_value=Response(200, json=_page(open_invoices or [])))
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/invoices\?.*status=PAID.*$").mock(
        return_value=Response(200, json=_page(paid_invoices or []))
    )
    # Recent invoices (no status param)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/invoices(\?.*)?$").mock(
        return_value=Response(200, json=_page(recent_invoices or []))
    )

    # AP
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/bills\?.*status=DRAFT.*$").mock(
        return_value=Response(200, json=_page(draft_bills or []))
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/bills\?.*status=SENT%2CPARTIALLY_PAID.*$"
    ).mock(return_value=Response(200, json=_page(open_bills or [])))
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/bills\?.*status=PAID.*$").mock(
        return_value=Response(200, json=_page(paid_bills or []))
    )
    # Recent bills (no status param)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/bills(\?.*)?$").mock(
        return_value=Response(200, json=_page(recent_bills or []))
    )

    # Payments (cash tile + recent)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/payments(\?.*)?$").mock(
        return_value=Response(200, json=_page(payments or recent_payments or []))
    )

    # Journal entries
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/journal_entries(\?.*)?$").mock(
        return_value=Response(200, json=_page(recent_je or []))
    )

    # Contacts
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/contacts(\?.*)?$").mock(
        return_value=Response(200, json=_page(recent_contacts or []))
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_dashboard_returns_200_and_title(respx_mock: respx.MockRouter) -> None:
    """GET / returns 200 and contains 'Dashboard' in the HTML."""
    _register_mocks(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    assert "<html" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_ar_tile(respx_mock: respx.MockRouter) -> None:
    """AR tile shows draft total, overdue count, and paid-this-month total."""
    draft = [_inv(id_="d001", number="INV-D001", status="DRAFT", total="500.00")]
    overdue = [
        _inv(id_="o001", number="INV-O001", status="SENT",
             due_date=_YESTERDAY, total="300.00"),
        _inv(id_="o002", number="INV-O002", status="PARTIALLY_PAID",
             due_date=_YESTERDAY, total="200.00"),
    ]
    paid = [_inv(id_="p001", number="INV-P001", status="PAID", total="1000.00")]

    _register_mocks(
        respx_mock,
        draft_invoices=draft,
        open_invoices=overdue,
        paid_invoices=paid,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # Draft total
    assert "500.00" in resp.text
    # Overdue count should show 2 somewhere
    assert "2" in resp.text
    # Overdue total 500.00 (300 + 200)
    assert "500.00" in resp.text
    # Paid total
    assert "1000.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_ap_tile(respx_mock: respx.MockRouter) -> None:
    """AP tile shows draft total, due-soon count, and paid-this-month total."""
    draft = [_bill(id_="bd001", number="BILL-D001", status="DRAFT", total="400.00")]
    open_ = [
        _bill(id_="bo001", number="BILL-O001", status="SENT",
              due_date=_IN_3_DAYS, total="250.00"),
    ]
    paid = [_bill(id_="bp001", number="BILL-P001", status="PAID", total="750.00")]

    _register_mocks(
        respx_mock,
        draft_bills=draft,
        open_bills=open_,
        paid_bills=paid,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # Draft total
    assert "400.00" in resp.text
    # Due-soon total
    assert "250.00" in resp.text
    # Paid total
    assert "750.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_cash_tile(respx_mock: respx.MockRouter) -> None:
    """Cash tile shows IN total, OUT total, and net (IN - OUT)."""
    pmt_in = _payment(
        id_="cin001", number="PAY-IN", direction="INCOMING",
        amount="1200.00", payment_date=_TODAY,
    )
    pmt_out = _payment(
        id_="cout001", number="PAY-OUT", direction="OUTGOING",
        amount="450.00", payment_date=_TODAY,
    )
    # Payment from last month — should NOT be counted.
    pmt_old = _payment(
        id_="cold001", number="PAY-OLD", direction="INCOMING",
        amount="9999.00", payment_date="2020-01-15",
    )

    _register_mocks(respx_mock, payments=[pmt_in, pmt_out, pmt_old])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "1200.00" in resp.text
    assert "450.00" in resp.text
    # Net = 1200 - 450 = 750
    assert "750.00" in resp.text
    # Old payment total must NOT appear in cash tile
    assert "9999.00" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_recent_activity(respx_mock: respx.MockRouter) -> None:
    """Recent activity list renders items mixed across entity types."""
    inv = _inv(id_="ri001", number="INV-R001", created_at="2026-04-23T10:00:00Z")
    bill = _bill(id_="rb001", number="BILL-R001", created_at="2026-04-22T10:00:00Z")
    pmt = _payment(id_="rp001", number="PAY-R001", created_at="2026-04-21T10:00:00Z")
    je = _je(id_="rj001", number="JE-R001", created_at="2026-04-20T10:00:00Z")
    contact = _contact(id_="rc001", name="Recent Corp", created_at="2026-04-19T10:00:00Z")

    _register_mocks(
        respx_mock,
        recent_invoices=[inv],
        recent_bills=[bill],
        recent_payments=[pmt],
        recent_je=[je],
        recent_contacts=[contact],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "INV-R001" in resp.text
    assert "BILL-R001" in resp.text
    assert "PAY-R001" in resp.text
    assert "JE-R001" in resp.text
    assert "Recent Corp" in resp.text
    # Entity-type badges
    assert "Invoice" in resp.text
    assert "Bill" in resp.text
    assert "Payment" in resp.text
    assert "Journal Entry" in resp.text
    assert "Contact" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_empty_data_no_errors(respx_mock: respx.MockRouter) -> None:
    """All zeros / empty lists — page renders 200 without division errors."""
    _register_mocks(respx_mock)  # all defaults are empty lists

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    # Zero totals must appear as 0.00
    assert "0.00" in resp.text
    # "No recent activity" message should be shown
    assert "No recent activity" in resp.text
