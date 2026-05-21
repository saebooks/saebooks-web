"""Dashboard home page — Lane D cycle 25.

GET /  → render the dashboard, computing AR/AP/cash tiles and recent activity
         from parallel API calls via asyncio.gather.

Tiles:
  AR at a glance      — draft invoices, open (POSTED, overdue computed in Python),
                        paid-this-month
  AP at a glance      — draft bills, due-within-7-days bills, paid-this-month bills
  Cash movement       — this month's payment IN total, OUT total, net
  Weekly takings      — INCOMING payment totals: this week vs prior week + delta.
                        Computed from the same payments page already fetched for the
                        cash tile (no extra API call).  Relevant for hospitality
                        personas (micro-cafe).
                        Deferred: casual-hours pending and till-variance trends
                        require payroll v2 (STP Phase 2 / Batch JJ) — not in v1.
  Recent activity     — last 5 items across invoices/bills/payments/journal_entries/
                        contacts ordered by created_at DESC

Status note: InvoiceStatus and BillStatus enums are DRAFT/POSTED/VOIDED only.
There is no SENT, PARTIALLY_PAID, or OVERDUE status.  Overdue is computed in
Python: invoice["due_date"] < today AND invoice["status"] == "POSTED".

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return token if present, else None."""
    return request.session.get("api_token")


def _to_decimal(value: object) -> Decimal:
    """Coerce a value to Decimal, returning 0 on failure."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _this_month_range() -> tuple[str, str]:
    """Return (YYYY-MM-01, YYYY-MM-DD) strings for the current month."""
    today = date.today()
    first = today.replace(day=1)
    return first.isoformat(), today.isoformat()


def _au_fy_range() -> tuple[str, str]:
    """Return (from, to) ISO date strings for the current Australian FY.

    Australian FY runs 1 July – 30 June.  If today is before 1 July of the
    current calendar year the FY started on 1 July of the prior year.
    """
    today = date.today()
    if today.month >= 7:
        fy_start = date(today.year, 7, 1)
    else:
        fy_start = date(today.year - 1, 7, 1)
    return fy_start.isoformat(), today.isoformat()


def _week_range(offset_weeks: int = 0) -> tuple[str, str]:
    """Return (Mon, end) ISO date strings for a calendar week.

    offset_weeks=0  → this week: Monday of current week through today.
    offset_weeks=-1 → last week: Monday through Sunday of prior week.
    """
    today = date.today()
    mon = today - timedelta(days=today.weekday())
    if offset_weeks == 0:
        return mon.isoformat(), today.isoformat()
    week_mon = mon + timedelta(weeks=offset_weeks)
    week_end = week_mon + timedelta(days=6)
    return week_mon.isoformat(), week_end.isoformat()


# ---------------------------------------------------------------------------
# API fetch helpers — each returns the parsed JSON or a safe default.
# ---------------------------------------------------------------------------


async def _fetch_json(client, path: str, params: dict | None = None) -> dict:
    """GET path and return parsed JSON; return empty dict on non-2xx."""
    try:
        resp = await client.get(path, params=params or {})
        if resp.is_success:
            return resp.json()
    except Exception:
        pass
    return {}


async def _fetch_items(client, path: str, params: dict | None = None) -> list[dict]:
    """GET path and return items list; return [] on failure."""
    payload = await _fetch_json(client, path, params)
    return payload.get("items", [])


async def _empty_list() -> list[dict]:
    """Async no-op that returns an empty list.

    Used as a gather slot when there is no valid API call to make (e.g. the
    PAID status does not exist in the InvoiceStatus / BillStatus enums).
    """
    return []


# ---------------------------------------------------------------------------
# Tile computation helpers — pure Python, no I/O.
# ---------------------------------------------------------------------------


def _ar_tile(
    draft_invoices: list[dict],
    open_invoices: list[dict],
    paid_invoices: list[dict],
) -> dict:
    """Compute AR tile data from three pre-fetched invoice lists."""
    today = date.today()

    draft_count = len(draft_invoices)
    draft_total = sum(_to_decimal(inv.get("total", 0)) for inv in draft_invoices)

    # Overdue = POSTED + due_date past + still has outstanding balance.
    # An invoice that's been paid in full is no longer overdue, even if its
    # due_date is in the past.
    overdue = [
        inv for inv in open_invoices
        if inv.get("due_date")
        and date.fromisoformat(str(inv["due_date"])) < today
        and _to_decimal(inv.get("amount_paid", 0)) < _to_decimal(inv.get("total", 0))
    ]
    overdue_count = len(overdue)
    overdue_total = sum(
        _to_decimal(inv.get("total", 0)) - _to_decimal(inv.get("amount_paid", 0))
        for inv in overdue
    )

    # Outstanding = POSTED invoices with amount_paid < total.  Replaces the
    # never-populated "paid this month" tile (the InvoiceStatus enum has no
    # PAID, so paid_invoices is always []).  Outstanding is the operator's
    # actual question: how much money are we waiting on.
    outstanding = [
        inv for inv in open_invoices
        if _to_decimal(inv.get("amount_paid", 0)) < _to_decimal(inv.get("total", 0))
    ]
    paid_count = len(outstanding)
    paid_total = sum(
        _to_decimal(inv.get("total", 0)) - _to_decimal(inv.get("amount_paid", 0))
        for inv in outstanding
    )

    return {
        "draft_count": draft_count,
        "draft_total": draft_total,
        "overdue_count": overdue_count,
        "overdue_total": overdue_total,
        "paid_count": paid_count,
        "paid_total": paid_total,
    }


def _ap_tile(
    draft_bills: list[dict],
    open_bills: list[dict],
    paid_bills: list[dict],
) -> dict:
    """Compute AP tile data from three pre-fetched bill lists."""
    today = date.today()
    in_7 = today + timedelta(days=7)

    draft_count = len(draft_bills)
    draft_total = sum(_to_decimal(b.get("total", 0)) for b in draft_bills)

    # Due-soon = POSTED + due_date within next 7 days + still has outstanding
    # balance (a bill that's been paid in full doesn't need our attention even
    # if its due_date is upcoming).
    due_soon = [
        b for b in open_bills
        if b.get("due_date")
        and today <= date.fromisoformat(str(b["due_date"])) <= in_7
        and _to_decimal(b.get("amount_paid", 0)) < _to_decimal(b.get("total", 0))
    ]
    due_soon_count = len(due_soon)
    due_soon_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in due_soon
    )

    # Outstanding = POSTED bills with amount_paid < total.  Replaces the
    # never-populated "paid this month" tile (BillStatus has no PAID status).
    outstanding = [
        b for b in open_bills
        if _to_decimal(b.get("amount_paid", 0)) < _to_decimal(b.get("total", 0))
    ]
    paid_count = len(outstanding)
    paid_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in outstanding
    )

    return {
        "draft_count": draft_count,
        "draft_total": draft_total,
        "due_soon_count": due_soon_count,
        "due_soon_total": due_soon_total,
        "paid_count": paid_count,
        "paid_total": paid_total,
    }


def _cash_tile(payments: list[dict]) -> dict:
    """Compute cash-movement totals for the current month + 30-day daily series.

    Returns month totals plus per-day arrays the dashboard sparkline can plot
    directly (no client-side synthesis of fake data). Days with no payment
    contribute zero amounts so the series is always 30 elements long.
    """
    from datetime import date as _date, timedelta as _td

    month_start, month_end = _this_month_range()
    today = _date.today()
    window_start = today - _td(days=29)

    daily_in: dict[str, Decimal] = {}
    daily_out: dict[str, Decimal] = {}
    dates: list[str] = []
    for i in range(30):
        d = (window_start + _td(days=i)).isoformat()
        dates.append(d)
        daily_in[d] = Decimal("0")
        daily_out[d] = Decimal("0")

    in_total = Decimal("0")
    out_total = Decimal("0")

    for pmt in payments:
        pmt_date = pmt.get("payment_date", "")
        if not pmt_date:
            continue
        pmt_date_str = str(pmt_date)
        amount = _to_decimal(pmt.get("amount", 0))
        direction = pmt.get("direction", "")

        if month_start <= pmt_date_str <= month_end:
            if direction == "INCOMING":
                in_total += amount
            elif direction == "OUTGOING":
                out_total += amount

        if pmt_date_str in daily_in:
            if direction == "INCOMING":
                daily_in[pmt_date_str] += amount
            elif direction == "OUTGOING":
                daily_out[pmt_date_str] += amount

    series_in = [float(daily_in[d]) for d in dates]
    series_out = [float(daily_out[d]) for d in dates]
    cum_net: list[float] = []
    running = 0.0
    for i in range(len(dates)):
        running += series_in[i] - series_out[i]
        cum_net.append(running)

    return {
        "in_total": in_total,
        "out_total": out_total,
        "net": in_total - out_total,
        "daily_in": series_in,
        "daily_out": series_out,
        "daily_net": cum_net,
        "daily_dates": dates,
    }


def _weekly_takings_tile(payments: list[dict]) -> dict:
    """Compute weekly takings (INCOMING payments) for this week vs last week.

    Reuses the payments list already fetched for the cash tile — no extra API call.
    Change percentage is None when last week total is zero (avoid div-by-zero).
    """
    this_start, this_end = _week_range(0)
    last_start, last_end = _week_range(-1)

    this_week = Decimal("0")
    last_week = Decimal("0")

    for pmt in payments:
        if pmt.get("direction") != "INCOMING":
            continue
        pmt_date = str(pmt.get("payment_date", ""))
        if not pmt_date:
            continue
        amount = _to_decimal(pmt.get("amount", 0))
        if this_start <= pmt_date <= this_end:
            this_week += amount
        elif last_start <= pmt_date <= last_end:
            last_week += amount

    change = this_week - last_week
    change_pct: float | None = None
    if last_week != Decimal("0"):
        change_pct = float(change / last_week * 100)

    return {
        "this_week": this_week,
        "last_week": last_week,
        "change": change,
        "change_pct": change_pct,
        "this_start": this_start,
        "this_end": this_end,
        "last_start": last_start,
        "last_end": last_end,
    }


def _gst_tile(ytd_data: dict) -> dict:
    """Extract GST turnover data from the ytd_turnover API response.

    Returns a safe dict regardless of whether the API call succeeded.
    threshold_crossed=False, threshold_approaching=False, and
    ytd_turnover=0.0 are the safe defaults.
    """
    ytd = _to_decimal(ytd_data.get("ytd_turnover", 0))
    threshold = _to_decimal(ytd_data.get("threshold", 75000))
    threshold_crossed = bool(ytd_data.get("threshold_crossed", False))
    threshold_approaching = bool(ytd_data.get("threshold_approaching", False))
    pct = float(ytd / threshold * 100) if threshold else 0.0
    return {
        "ytd_turnover": ytd,
        "threshold": threshold,
        "threshold_crossed": threshold_crossed,
        "threshold_approaching": threshold_approaching,
        "pct": min(pct, 100.0),
        "fy_start": ytd_data.get("fy_start", ""),
        "fy_end": ytd_data.get("fy_end", ""),
    }


def _recent_activity(
    invoices: list[dict],
    bills: list[dict],
    payments: list[dict],
    journal_entries: list[dict],
    contacts: list[dict],
) -> list[dict]:
    """Merge first-page items from each entity, sort by created_at DESC, return top 5."""
    tagged: list[dict] = []

    # Build a contact lookup so invoice/bill/payment rows can show the
    # counterparty name alongside the document number.
    cmap: dict[str, str] = {c.get("id"): c.get("name") or "" for c in contacts if c.get("id")}

    def _with_contact(number: str, cid: str | None) -> str:
        name = cmap.get(cid or "") or ""
        if number and name:
            return f"{number} — {name}"
        return number or name or ""

    for item in invoices:
        tagged.append({"entity_type": "Invoice", "item": item,
                        "label": _with_contact(item.get("number", ""), item.get("contact_id")) or item.get("id", ""),
                        "url": f"/invoices/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in bills:
        tagged.append({"entity_type": "Bill", "item": item,
                        "label": _with_contact(item.get("number", ""), item.get("contact_id")) or item.get("id", ""),
                        "url": f"/bills/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in payments:
        num = item.get("number") or item.get("reference") or ""
        tagged.append({"entity_type": "Payment", "item": item,
                        "label": _with_contact(num, item.get("contact_id")) or item.get("id", ""),
                        "url": f"/payments/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in journal_entries:
        # JEs use `ref` (e.g. FIX-20260519-001, QT3194) — not `number`.
        ref = item.get("ref") or item.get("number") or ""
        desc = (item.get("description") or "").strip()
        label = ref
        if desc:
            label = f"{ref} — {desc[:60]}" if ref else desc[:80]
        tagged.append({"entity_type": "Journal Entry", "item": item,
                        "label": label or item.get("id", ""),
                        "url": f"/journal-entries/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in contacts:
        tagged.append({"entity_type": "Contact", "item": item,
                        "label": item.get("name") or item.get("id", ""),
                        "url": f"/contacts/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    tagged.sort(key=lambda x: x["created_at"], reverse=True)
    return tagged[:5]


# ---------------------------------------------------------------------------
# Catalogue-only widgets (default-hidden — user adds via Customise)
# ---------------------------------------------------------------------------


def _sales_pipeline(
    draft_invoices: list[dict],
    open_invoices: list[dict],
    payments: list[dict],
) -> list[dict]:
    """5-stage AR pipeline — matches the funnel shape used on /sales/overview."""
    today = date.today()
    window_start = (today - timedelta(days=29)).isoformat()

    draft_total = sum(_to_decimal(inv.get("total", 0)) for inv in draft_invoices)
    overdue = [
        inv for inv in open_invoices
        if inv.get("due_date")
        and date.fromisoformat(str(inv["due_date"])) < today
        and _to_decimal(inv.get("amount_paid", 0)) < _to_decimal(inv.get("total", 0))
    ]
    overdue_total = sum(
        _to_decimal(inv.get("total", 0)) - _to_decimal(inv.get("amount_paid", 0))
        for inv in overdue
    )
    unpaid = [
        inv for inv in open_invoices
        if _to_decimal(inv.get("amount_paid", 0)) < _to_decimal(inv.get("total", 0))
        and inv not in overdue
    ]
    unpaid_total = sum(
        _to_decimal(inv.get("total", 0)) - _to_decimal(inv.get("amount_paid", 0))
        for inv in unpaid
    )
    paid_30d = sum(
        _to_decimal(p.get("amount", 0)) for p in payments
        if p.get("direction") == "INCOMING"
        and str(p.get("payment_date", "")) >= window_start
    )
    return [
        {"label": "Drafts", "count": len(draft_invoices), "total": draft_total,
         "tone": "warm", "href": "/invoices?status=DRAFT", "icon": "file-text"},
        {"label": "Overdue", "count": len(overdue), "total": overdue_total,
         "tone": "neg", "href": "/invoices?status=POSTED", "icon": "alert-triangle"},
        {"label": "Unpaid", "count": len(unpaid), "total": unpaid_total,
         "tone": "sae", "href": "/invoices?status=POSTED", "icon": "clock"},
        {"label": "Paid · 30d", "count": None, "total": paid_30d,
         "tone": "pos", "href": "/payments?direction=INCOMING", "icon": "check-circle-2"},
    ]


def _bills_pipeline(
    draft_bills: list[dict],
    open_bills: list[dict],
    payments: list[dict],
) -> list[dict]:
    """5-stage AP pipeline — mirrors /expenses-overview funnel."""
    today = date.today()
    in_7 = today + timedelta(days=7)
    window_start = (today - timedelta(days=29)).isoformat()

    draft_total = sum(_to_decimal(b.get("total", 0)) for b in draft_bills)
    overdue = [
        b for b in open_bills
        if b.get("due_date")
        and date.fromisoformat(str(b["due_date"])) < today
        and _to_decimal(b.get("amount_paid", 0)) < _to_decimal(b.get("total", 0))
    ]
    overdue_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in overdue
    )
    due_soon = [
        b for b in open_bills
        if b.get("due_date")
        and today <= date.fromisoformat(str(b["due_date"])) <= in_7
        and _to_decimal(b.get("amount_paid", 0)) < _to_decimal(b.get("total", 0))
    ]
    due_soon_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in due_soon
    )
    unpaid = [
        b for b in open_bills
        if _to_decimal(b.get("amount_paid", 0)) < _to_decimal(b.get("total", 0))
        and b not in overdue and b not in due_soon
    ]
    unpaid_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in unpaid
    )
    paid_30d = sum(
        _to_decimal(p.get("amount", 0)) for p in payments
        if p.get("direction") == "OUTGOING"
        and str(p.get("payment_date", "")) >= window_start
    )
    return [
        {"label": "For review", "count": len(draft_bills), "total": draft_total,
         "tone": "warm", "href": "/bills?status=DRAFT", "icon": "file-text"},
        {"label": "Overdue", "count": len(overdue), "total": overdue_total,
         "tone": "neg", "href": "/bills?status=POSTED", "icon": "alert-triangle"},
        {"label": "Due soon", "count": len(due_soon), "total": due_soon_total,
         "tone": "warm", "href": "/bills?status=POSTED", "icon": "clock"},
        {"label": "Unpaid", "count": len(unpaid), "total": unpaid_total,
         "tone": "sae", "href": "/bills?status=POSTED", "icon": "hourglass"},
        {"label": "Paid · 30d", "count": None, "total": paid_30d,
         "tone": "pos", "href": "/payments?direction=OUTGOING", "icon": "check-circle-2"},
    ]


def _top_vendors_month(
    bills: list[dict],
    contacts: list[dict],
) -> list[dict]:
    """Top 5 suppliers by spend this calendar month.

    Aggregates from the bills already pre-fetched (DRAFT + POSTED first
    page). Sufficient for a catalogue widget; the full report lives at
    /expenses-overview.
    """
    month_start, month_end = _this_month_range()
    cmap: dict[str, str] = {c.get("id"): c.get("name") or "" for c in contacts if c.get("id")}

    by_supplier: dict[str, Decimal] = {}
    by_count: dict[str, int] = {}
    for b in bills:
        d = str(b.get("issue_date") or "")
        if not (month_start <= d <= month_end):
            continue
        cid = b.get("contact_id") or ""
        if not cid:
            continue
        by_supplier[cid] = by_supplier.get(cid, Decimal("0")) + _to_decimal(b.get("total", 0))
        by_count[cid] = by_count.get(cid, 0) + 1

    return [
        {
            "contact_id": cid,
            "name": cmap.get(cid, "—"),
            "total": tot,
            "count": by_count.get(cid, 0),
        }
        for cid, tot in sorted(by_supplier.items(), key=lambda x: x[1], reverse=True)[:5]
    ]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the dashboard home page.

    Fires all API calls in parallel via asyncio.gather, then computes tile
    data in Python and renders the template.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    fy_from, fy_to = _au_fy_range()

    async with api_client(request) as client:
        (
            draft_invoices_raw,
            open_invoices_raw,
            paid_inv_raw,
            draft_bills_raw,
            open_bills_raw,
            paid_bills_raw,
            payments_raw,
            recent_invoices_raw,
            recent_bills_raw,
            recent_payments_raw,
            recent_je_raw,
            recent_contacts_raw,
            ytd_turnover_raw,
            companies_raw,
            revenue_concentration_raw,
        ) = await asyncio.gather(
            # AR tile — open invoices are POSTED (overdue computed in Python)
            _fetch_items(client, "/api/v1/invoices",
                         {"status": "DRAFT", "page": 1, "page_size": 100}),
            _fetch_items(client, "/api/v1/invoices",
                         {"status": "POSTED", "page": 1, "page_size": 100}),
            # No PAID status in the enum — paid tile always returns empty list.
            _empty_list(),
            # AP tile — open bills are POSTED (due-soon computed in Python)
            _fetch_items(client, "/api/v1/bills",
                         {"status": "DRAFT", "page": 1, "page_size": 100}),
            _fetch_items(client, "/api/v1/bills",
                         {"status": "POSTED", "page": 1, "page_size": 100}),
            # No PAID status in the enum — paid tile always returns empty list.
            _empty_list(),
            # Cash tile — all payments, filter by month in Python
            _fetch_items(client, "/api/v1/payments",
                         {"page": 1, "page_size": 100}),
            # Recent activity
            _fetch_items(client, "/api/v1/invoices",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/bills",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/payments",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/journal_entries",
                         {"page": 1, "page_size": 500}),
            # page_size=200 so we have a contact name map for the Top
            # vendors catalogue widget. Recent-activity still trims to 5.
            _fetch_items(client, "/api/v1/contacts",
                         {"page": 1, "page_size": 200}),
            # GST turnover tile
            _fetch_json(client, "/api/v1/reports/ytd_turnover"),
            # PSI status from first active company
            _fetch_json(client, "/api/v1/companies", {"limit": 1, "offset": 0}),
            # Revenue concentration — PSI 80/20 warning (current AU FY)
            _fetch_json(client, "/api/v1/reports/revenue_by_customer",
                        {"from_date": fy_from, "to_date": fy_to}),
        )

    # Handle 401 edge case — if all lists are empty and the token is gone,
    # we let the page render empty rather than soft-looping.  The user can
    # navigate away; a future middleware improvement would catch this globally.

    ar = _ar_tile(draft_invoices_raw, open_invoices_raw, paid_inv_raw)
    ap = _ap_tile(draft_bills_raw, open_bills_raw, paid_bills_raw)
    cash = _cash_tile(payments_raw)
    takings = _weekly_takings_tile(payments_raw)
    recent = _recent_activity(
        recent_invoices_raw,
        recent_bills_raw,
        recent_payments_raw,
        recent_je_raw,
        recent_contacts_raw,
    )
    gst = _gst_tile(ytd_turnover_raw)

    # Catalogue widgets — default hidden, surfaced via "Add widget" dialog.
    sales_pipeline = _sales_pipeline(
        draft_invoices_raw, open_invoices_raw, payments_raw
    )
    bills_pipeline = _bills_pipeline(
        draft_bills_raw, open_bills_raw, payments_raw
    )
    top_vendors_month = _top_vendors_month(
        list(draft_bills_raw) + list(open_bills_raw),
        recent_contacts_raw,
    )

    first_company = (companies_raw.get("items") or [{}])[0]
    psi_status = first_company.get("psi_status", "unsure") or "unsure"
    company_name = first_company.get("legal_name") or first_company.get("name") or ""

    # Revenue concentration — used by PSI 80/20 dashboard banner
    concentration_warning = revenue_concentration_raw.get("concentration_warning", False)
    top_customer_pct = revenue_concentration_raw.get("top_customer_pct")
    top_customer_name = ""
    if revenue_concentration_raw.get("rows"):
        top_customer_name = revenue_concentration_raw["rows"][0].get("contact_name", "")

    return _TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "ar": ar,
            "ap": ap,
            "cash": cash,
            "takings": takings,
            "recent": recent,
            "gst": gst,
            "psi_status": psi_status,
            "concentration_warning": concentration_warning,
            "top_customer_pct": top_customer_pct,
            "top_customer_name": top_customer_name,
            "fy_from": fy_from,
            "fy_to": fy_to,
            "company_name": company_name,
            "sales_pipeline": sales_pipeline,
            "bills_pipeline": bills_pipeline,
            "top_vendors_month": top_vendors_month,
            "today_iso": date.today().isoformat(),
        },
    )
