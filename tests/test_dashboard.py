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
import httpx
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


def _ytd_response(
    ytd_turnover: float = 0.0,
    threshold: float = 75000.0,
    threshold_crossed: bool = False,
    threshold_approaching: bool = False,
    fy_start: str = "2025-07-01",
    fy_end: str = "2026-06-30",
) -> dict:
    return {
        "ytd_turnover": ytd_turnover,
        "threshold": threshold,
        "threshold_crossed": threshold_crossed,
        "threshold_approaching": threshold_approaching,
        "fy_start": fy_start,
        "fy_end": fy_end,
    }


def _register_mocks(
    respx_mock: respx.MockRouter,
    *,
    draft_invoices: list | None = None,
    open_invoices: list | None = None,
    draft_bills: list | None = None,
    open_bills: list | None = None,
    payments: list | None = None,
    recent_invoices: list | None = None,
    recent_bills: list | None = None,
    recent_payments: list | None = None,
    recent_je: list | None = None,
    recent_contacts: list | None = None,
    ytd_data: dict | None = None,
    register_shared_side_fetches: bool = True,
) -> None:
    """Register all API mocks that the dashboard fires in parallel.

    The dashboard now fetches:
      - invoices?status=DRAFT    (AR draft tile)
      - invoices?status=POSTED   (AR open/overdue tile — overdue computed in Python)
      - bills?status=DRAFT       (AP draft tile)
      - bills?status=POSTED      (AP open/due-soon tile — due-soon computed in Python)
      - payments                 (cash tile + recent)
      - invoices (no status)     (recent activity)
      - bills (no status)        (recent activity)
      - payments (no status)     (recent activity — shared with cash tile mock)
      - journal_entries          (recent activity)
      - contacts                 (recent activity)
      - reports/ytd_turnover     (GST turnover tile + banner)

    No PAID status exists in InvoiceStatus/BillStatus — paid tiles always zero.

    respx matches by first-registered route; most-specific (status=) patterns
    are registered before the catch-all no-status patterns.
    """
    # AR
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/invoices\?.*status=DRAFT.*$").mock(
        return_value=Response(200, json=_page(draft_invoices or []))
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/invoices\?.*status=POSTED.*$"
    ).mock(return_value=Response(200, json=_page(open_invoices or [])))
    # Recent invoices (no status param)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/invoices(\?.*)?$").mock(
        return_value=Response(200, json=_page(recent_invoices or []))
    )

    # AP
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/bills\?.*status=DRAFT.*$").mock(
        return_value=Response(200, json=_page(draft_bills or []))
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/bills\?.*status=POSTED.*$"
    ).mock(return_value=Response(200, json=_page(open_bills or [])))
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

    # YTD turnover (GST threshold)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/ytd_turnover(\?.*)?$").mock(
        return_value=Response(200, json=ytd_data or _ytd_response())
    )
    # Companies (PSI status; also hit by CompanyContextMiddleware),
    # revenue concentration, and the module catalogue — since the M2
    # degrade layer, an unmocked fetch degrades the compliance tile
    # instead of being silently swallowed, hiding the GST/PSI banners
    # these tests assert on. Callers that register their OWN
    # companies/tax_codes mocks (test_jurisdiction_gating,
    # test_i18n_concurrency) pass register_shared_side_fetches=False so
    # these catch-alls can't shadow their jurisdiction-bearing payloads.
    if register_shared_side_fetches:
        respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$").mock(
            return_value=Response(200, json={"items": []})
        )
        respx_mock.get(
            url__regex=rf"^{_API_BASE}/api/v1/reports/revenue_by_customer(\?.*)?$"
        ).mock(return_value=Response(200, json={"rows": []}))
        respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
            return_value=Response(200, json={"modules": []})
        )
        # Enterprise KPI catalogue-widget fetches (aged AR/AP, MTD P&L,
        # budget vs actual) — quiet defaults so the widgets render empty
        # rather than degraded in unrelated tests.
        respx_mock.get(
            url__regex=rf"^{_API_BASE}/api/v1/reports/aged_receivables(\?.*)?$"
        ).mock(return_value=Response(200, json={"buckets": [], "contacts": [], "totals": {}}))
        respx_mock.get(
            url__regex=rf"^{_API_BASE}/api/v1/reports/aged_payables(\?.*)?$"
        ).mock(return_value=Response(200, json={"buckets": [], "contacts": [], "totals": {}}))
        respx_mock.get(
            url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss(\?.*)?$"
        ).mock(return_value=Response(200, json={"income": {}, "expenses": {}, "net_profit": 0}))
        respx_mock.get(
            url__regex=rf"^{_API_BASE}/api/v1/reports/budget_vs_actual(\?.*)?$"
        ).mock(return_value=Response(200, json={"lines": [], "total_budget": 0, "total_actual": 0, "total_variance": 0}))


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
    """AR tile shows draft total and overdue count (POSTED past due date).

    The PAID status does not exist; paid tile will always show 0.
    Overdue is computed in Python: status==POSTED AND due_date < today.
    """
    draft = [_inv(id_="d001", number="INV-D001", status="DRAFT", total="500.00")]
    # Both invoices are POSTED and past due — both should be counted as overdue.
    overdue = [
        _inv(id_="o001", number="INV-O001", status="POSTED",
             due_date=_YESTERDAY, total="300.00"),
        _inv(id_="o002", number="INV-O002", status="POSTED",
             due_date=_YESTERDAY, total="200.00"),
    ]

    _register_mocks(
        respx_mock,
        draft_invoices=draft,
        open_invoices=overdue,
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


@pytest.mark.anyio
@respx.mock
async def test_dashboard_ap_tile(respx_mock: respx.MockRouter) -> None:
    """AP tile shows draft total and due-soon count (POSTED bills due within 7 days).

    The PAID status does not exist; paid tile will always show 0.
    Due-soon is computed in Python: status==POSTED AND today <= due_date <= today+7.
    """
    draft = [_bill(id_="bd001", number="BILL-D001", status="DRAFT", total="400.00")]
    open_ = [
        _bill(id_="bo001", number="BILL-O001", status="POSTED",
              due_date=_IN_3_DAYS, total="250.00"),
    ]

    _register_mocks(
        respx_mock,
        draft_bills=draft,
        open_bills=open_,
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
    # money() now renders a thousands separator.
    assert "1,200.00" in resp.text
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
    # "no recent activity" message should be shown (now rendered lower-case,
    # wrapped in em-dashes: "— no recent activity —")
    assert "no recent activity" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_gst_tile_below_threshold(respx_mock: respx.MockRouter) -> None:
    """GST tile renders YTD turnover and shows 'Under threshold' when below $75k."""
    _register_mocks(
        respx_mock,
        ytd_data=_ytd_response(ytd_turnover=45000.0, threshold_crossed=False),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # money() now renders a thousands separator.
    assert "45,000.00" in resp.text
    # Chip label is rendered lower-case: "GST · under threshold".
    assert "under threshold" in resp.text
    # Banner must NOT appear when below threshold
    assert "you must register with the ATO" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_gst_banner_above_threshold(respx_mock: respx.MockRouter) -> None:
    """GST banner appears and shows registration warning when turnover >= $75k."""
    _register_mocks(
        respx_mock,
        ytd_data=_ytd_response(ytd_turnover=78000.0, threshold_crossed=True),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # money() now renders a thousands separator.
    assert "78,000.00" in resp.text
    # Banner heading is rendered lower-case: "GST registration threshold crossed".
    assert "threshold crossed" in resp.text
    assert "You must register with the ATO within 21 days" in resp.text
    assert "Register with ATO" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_gst_banner_approaching_threshold(
    respx_mock: respx.MockRouter,
) -> None:
    """Amber approaching-threshold banner appears at 80-99% of $75k."""
    _register_mocks(
        respx_mock,
        ytd_data=_ytd_response(
            ytd_turnover=62000.0,
            threshold_crossed=False,
            threshold_approaching=True,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # money() now renders a thousands separator.
    assert "62,000.00" in resp.text
    assert "Approaching GST registration threshold" in resp.text
    assert "ATO registration info" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_weekly_takings_tile(respx_mock: respx.MockRouter) -> None:
    """Weekly Takings tile shows this-week and prior-week INCOMING totals + delta.

    Uses INCOMING payments dated within this calendar week and the prior Mon-Sun.
    OUTGOING payments must be excluded from takings.
    """
    from datetime import date, timedelta

    today = date.today()
    mon_this = today - timedelta(days=today.weekday())  # Monday this week
    mon_last = mon_this - timedelta(weeks=1)            # Monday last week
    wed_last = mon_last + timedelta(days=2)             # Wednesday last week

    # This week: two INCOMING payments totalling 820.00
    pmt_tw1 = _payment(
        id_="tw001", number="PAY-TW1", direction="INCOMING",
        amount="500.00", payment_date=mon_this.isoformat(),
    )
    pmt_tw2 = _payment(
        id_="tw002", number="PAY-TW2", direction="INCOMING",
        amount="320.00", payment_date=today.isoformat(),
    )
    # This week: OUTGOING — must NOT count toward takings
    pmt_tw_out = _payment(
        id_="tw003", number="PAY-TW3", direction="OUTGOING",
        amount="9999.00", payment_date=today.isoformat(),
    )
    # Last week: one INCOMING payment of 600.00
    pmt_lw1 = _payment(
        id_="lw001", number="PAY-LW1", direction="INCOMING",
        amount="600.00", payment_date=wed_last.isoformat(),
    )

    _register_mocks(respx_mock, payments=[pmt_tw1, pmt_tw2, pmt_tw_out, pmt_lw1])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # The "Weekly takings" heading must appear (rendered lower-case now)
    assert "Weekly takings" in resp.text
    # This-week total: 500 + 320 = 820
    assert "820.00" in resp.text
    # Prior-week total: 600
    assert "600.00" in resp.text
    # Change percentage only is rendered now (+36.7%); the flat $220 delta
    # amount is no longer shown separately.
    assert "36.7%" in resp.text
    # 820.00 this-week total can only be correct if OUTGOING was excluded from takings


@pytest.mark.anyio
@respx.mock
async def test_dashboard_psi_banner_shown_when_unsure(respx_mock: respx.MockRouter) -> None:
    """Dashboard shows PSI reminder banner when company psi_status is 'unsure'."""
    _register_mocks(respx_mock)
    # Override the companies endpoint to return a company with psi_status=unsure
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$"
    ).mock(return_value=Response(200, json={
        "items": [{"id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "psi_status": "unsure"}],
        "total": 1,
    }))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # Compliance-notes banner copy for psi_status == "unsure" (the "80/20
    # rule" / "Personal Services Income" phrasing belongs to a separate
    # revenue-concentration banner not exercised by this test).
    assert "PSI classification not set" in resp.text
    assert "Set PSI status" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_psi_banner_hidden_when_set(respx_mock: respx.MockRouter) -> None:
    """Dashboard does NOT show PSI banner when psi_status is 'yes' or 'no'."""
    _register_mocks(respx_mock)
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$"
    ).mock(return_value=Response(200, json={
        "items": [{"id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "psi_status": "no"}],
        "total": 1,
    }))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "PSI status unset" not in resp.text


# ---------------------------------------------------------------------------
# Enterprise KPI catalogue widgets (M2 enterprise views)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_dashboard_enterprise_kpi_tiles_render(respx_mock: respx.MockRouter) -> None:
    """Aged AR/AP, MTD P&L and budget-vs-actual widgets render from report data."""
    _register_mocks(respx_mock)
    # Override the quiet defaults with real payloads (respx: last registered
    # for the same pattern wins on re-registration via route replacement).
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/aged_receivables(\?.*)?$"
    ).mock(
        return_value=Response(
            200,
            json={
                "as_of_date": "2026-07-18",
                "buckets": ["Current", "30d", "60d"],
                "contacts": [],
                "totals": {"Current": "100.00", "30d": "50.00", "60d": "0", "total": "150.00"},
            },
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/aged_payables(\?.*)?$"
    ).mock(
        return_value=Response(
            200,
            json={
                "as_of_date": "2026-07-18",
                "buckets": ["Current", "30d", "60d"],
                "contacts": [],
                "totals": {"Current": "40.00", "30d": "0", "60d": "0", "total": "40.00"},
            },
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss(\?.*)?$"
    ).mock(
        return_value=Response(
            200,
            json={
                "income": {"INCOME": [{"account_name": "Sales", "amount": "1000.00"}]},
                "expenses": {"EXPENSE": [{"account_name": "Rent", "amount": "400.00"}]},
                "net_profit": "600.00",
            },
        )
    )
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/budget_vs_actual(\?.*)?$"
    ).mock(
        return_value=Response(
            200,
            json={
                "lines": [
                    {"account_name": "Rent", "budget": "500.00", "actual": "400.00", "variance": "100.00"},
                ],
                "total_budget": "500.00",
                "total_actual": "400.00",
                "total_variance": "100.00",
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert 'data-widget="aged-ar-ap"' in resp.text
    assert 'data-widget="pl-snapshot"' in resp.text
    assert 'data-widget="budget-vs-actual"' in resp.text
    # P&L snapshot numbers made it through.
    assert "60%" in resp.text or "60.0%" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_enterprise_kpi_tile_degrades_alone(
    respx_mock: respx.MockRouter,
) -> None:
    """Aged-report fetch down → only the aged widget degrades; page is 200."""
    _register_mocks(respx_mock)
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/aged_receivables(\?.*)?$"
    ).mock(side_effect=httpx.ConnectError("aged report down"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # The degraded panel appears inside the aged widget…
    aged_start = resp.text.index('data-widget="aged-ar-ap"')
    pl_start = resp.text.index('data-widget="pl-snapshot"')
    assert "data-degraded-panel" in resp.text[aged_start:pl_start]
    # …and the P&L widget still renders normally.
    assert "data-degraded-panel" not in resp.text[pl_start:]
