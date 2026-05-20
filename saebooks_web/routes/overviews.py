"""Section overview landing pages — Sales, Expenses, Inventory, GST.

Each /overview is a rich dashboard for one slice of the system:
  /sales/overview        — AR, sales MTD/YTD, top customers, recent invoices
  /expenses-overview     — AP, expense MTD/YTD, top suppliers, recent bills/expenses
  /inventory/overview    — Item count, low-stock flags, recent activity (stub)
  /gst/overview          — GST turnover, BAS period, tax-code breakdown, link to BAS

These pages reuse the same helpers + API endpoints as dashboard.py so they
stay consistent.  Each fires its API calls in parallel via asyncio.gather and
falls back to safe empty defaults when an endpoint is missing.

Auth guard: redirect to /login (303) if no session token (matches dashboard).
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
    return request.session.get("api_token")


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _this_month_range() -> tuple[str, str]:
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def _au_fy_range() -> tuple[str, str]:
    today = date.today()
    if today.month >= 7:
        fy_start = date(today.year, 7, 1)
    else:
        fy_start = date(today.year - 1, 7, 1)
    return fy_start.isoformat(), today.isoformat()


def _last_30d_range() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=29)).isoformat(), today.isoformat()


async def _fetch_json(client, path: str, params: dict | None = None) -> dict:
    try:
        resp = await client.get(path, params=params or {})
        if resp.is_success:
            return resp.json()
    except Exception:
        pass
    return {}


async def _fetch_items(client, path: str, params: dict | None = None) -> list[dict]:
    payload = await _fetch_json(client, path, params)
    return payload.get("items", [])


def _name_map(contacts: list[dict]) -> dict[str, str]:
    return {c.get("id"): c.get("name") or "" for c in contacts if c.get("id")}


# ─────────────────────────────────────────────────────────────────────────
# /sales/overview
# ─────────────────────────────────────────────────────────────────────────


@router.get("/sales/overview", response_class=HTMLResponse, response_model=None)
async def sales_overview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()
    month_start, month_end = _this_month_range()
    fy_from, fy_to = _au_fy_range()

    async with api_client(request) as client:
        (
            draft_invoices,
            open_invoices,
            recent_invoices,
            recent_quotes,
            recent_credit_notes,
            recent_payments_in,
            top_customers_data,
            contacts_raw,
        ) = await asyncio.gather(
            _fetch_items(client, "/api/v1/invoices",
                         {"status": "DRAFT", "page": 1, "page_size": 200}),
            _fetch_items(client, "/api/v1/invoices",
                         {"status": "POSTED", "page": 1, "page_size": 500}),
            _fetch_items(client, "/api/v1/invoices",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/quotes",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/credit-notes",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/payments",
                         {"direction": "INCOMING", "page": 1, "page_size": 200}),
            _fetch_json(client, "/api/v1/reports/revenue_by_customer",
                        {"from_date": fy_from, "to_date": fy_to}),
            _fetch_items(client, "/api/v1/contacts",
                         {"page": 1, "page_size": 500}),
        )

    cmap = _name_map(contacts_raw)

    # AR metrics
    draft_total = sum(_to_decimal(i.get("total", 0)) for i in draft_invoices)
    outstanding = [
        i for i in open_invoices
        if _to_decimal(i.get("amount_paid", 0)) < _to_decimal(i.get("total", 0))
    ]
    outstanding_total = sum(
        _to_decimal(i.get("total", 0)) - _to_decimal(i.get("amount_paid", 0))
        for i in outstanding
    )
    overdue = [
        i for i in outstanding
        if i.get("due_date") and date.fromisoformat(str(i["due_date"])) < today
    ]
    overdue_total = sum(
        _to_decimal(i.get("total", 0)) - _to_decimal(i.get("amount_paid", 0))
        for i in overdue
    )

    # Sales MTD / YTD (POSTED invoices in the period)
    sales_mtd = sum(
        _to_decimal(i.get("total", 0))
        for i in open_invoices
        if month_start <= str(i.get("invoice_date", "")) <= month_end
    )
    sales_ytd = sum(
        _to_decimal(i.get("total", 0))
        for i in open_invoices
        if fy_from <= str(i.get("invoice_date", "")) <= fy_to
    )

    # 30-day daily sales series
    win_start, win_end = _last_30d_range()
    daily_buckets: dict[str, Decimal] = {}
    dates: list[str] = []
    for i in range(30):
        d = (date.fromisoformat(win_start) + timedelta(days=i)).isoformat()
        dates.append(d)
        daily_buckets[d] = Decimal("0")
    for inv in open_invoices:
        d = str(inv.get("invoice_date", ""))
        if d in daily_buckets:
            daily_buckets[d] += _to_decimal(inv.get("total", 0))
    daily_sales = [float(daily_buckets[d]) for d in dates]

    # Receipts MTD (incoming payments)
    receipts_mtd = sum(
        _to_decimal(p.get("amount", 0))
        for p in recent_payments_in
        if month_start <= str(p.get("payment_date", "")) <= month_end
    )

    # Top customers — from FY revenue report
    top_customers = (top_customers_data.get("rows") or [])[:5]

    # Aging buckets
    def _bucket_days(inv: dict) -> int:
        dd = inv.get("due_date")
        if not dd:
            return 0
        try:
            return (today - date.fromisoformat(str(dd))).days
        except Exception:
            return 0

    aging = {"current": Decimal("0"), "d_1_30": Decimal("0"),
             "d_31_60": Decimal("0"), "d_61_90": Decimal("0"),
             "d_90_plus": Decimal("0")}
    for inv in outstanding:
        owed = _to_decimal(inv.get("total", 0)) - _to_decimal(inv.get("amount_paid", 0))
        days = _bucket_days(inv)
        if days <= 0:
            aging["current"] += owed
        elif days <= 30:
            aging["d_1_30"] += owed
        elif days <= 60:
            aging["d_31_60"] += owed
        elif days <= 90:
            aging["d_61_90"] += owed
        else:
            aging["d_90_plus"] += owed

    ctx = {
        "request": request,
        "today_iso": today.isoformat(),
        "month_start": month_start,
        "month_end": month_end,
        "fy_from": fy_from,
        "fy_to": fy_to,
        "draft_count": len(draft_invoices),
        "draft_total": draft_total,
        "outstanding_count": len(outstanding),
        "outstanding_total": outstanding_total,
        "overdue_count": len(overdue),
        "overdue_total": overdue_total,
        "sales_mtd": sales_mtd,
        "sales_ytd": sales_ytd,
        "receipts_mtd": receipts_mtd,
        "daily_sales": daily_sales,
        "daily_dates": dates,
        "top_customers": top_customers,
        "recent_invoices": recent_invoices,
        "recent_quotes": recent_quotes,
        "recent_credit_notes": recent_credit_notes,
        "recent_payments_in": recent_payments_in[:10],
        "contacts_by_id": cmap,
        "aging": aging,
    }
    return _TEMPLATES.TemplateResponse(request, "overviews/sales.html", ctx)


# ─────────────────────────────────────────────────────────────────────────
# /expenses-overview
# ─────────────────────────────────────────────────────────────────────────


@router.get("/expenses-overview", response_class=HTMLResponse, response_model=None)
async def expenses_overview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()
    month_start, month_end = _this_month_range()
    fy_from, fy_to = _au_fy_range()

    async with api_client(request) as client:
        (
            draft_bills,
            open_bills,
            recent_bills,
            recent_expenses,
            recent_pos,
            recent_payments_out,
            contacts_raw,
        ) = await asyncio.gather(
            _fetch_items(client, "/api/v1/bills",
                         {"status": "DRAFT", "page": 1, "page_size": 200}),
            _fetch_items(client, "/api/v1/bills",
                         {"status": "POSTED", "page": 1, "page_size": 500}),
            _fetch_items(client, "/api/v1/bills",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/expenses",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/purchase_orders",
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/payments",
                         {"direction": "OUTGOING", "page": 1, "page_size": 200}),
            _fetch_items(client, "/api/v1/contacts",
                         {"page": 1, "page_size": 500}),
        )

    cmap = _name_map(contacts_raw)

    # AP metrics
    draft_total = sum(_to_decimal(b.get("total", 0)) for b in draft_bills)
    outstanding = [
        b for b in open_bills
        if _to_decimal(b.get("amount_paid", 0)) < _to_decimal(b.get("total", 0))
    ]
    outstanding_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in outstanding
    )
    in_7 = today + timedelta(days=7)
    due_soon = [
        b for b in outstanding
        if b.get("due_date")
        and today <= date.fromisoformat(str(b["due_date"])) <= in_7
    ]
    due_soon_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in due_soon
    )
    overdue = [
        b for b in outstanding
        if b.get("due_date") and date.fromisoformat(str(b["due_date"])) < today
    ]
    overdue_total = sum(
        _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        for b in overdue
    )

    # Spend MTD / YTD = bills posted in window
    spend_mtd = sum(
        _to_decimal(b.get("total", 0))
        for b in open_bills
        if month_start <= str(b.get("bill_date", "")) <= month_end
    )
    spend_ytd = sum(
        _to_decimal(b.get("total", 0))
        for b in open_bills
        if fy_from <= str(b.get("bill_date", "")) <= fy_to
    )

    # 30-day daily spend series
    win_start, win_end = _last_30d_range()
    daily_buckets: dict[str, Decimal] = {}
    dates: list[str] = []
    for i in range(30):
        d = (date.fromisoformat(win_start) + timedelta(days=i)).isoformat()
        dates.append(d)
        daily_buckets[d] = Decimal("0")
    for b in open_bills:
        d = str(b.get("bill_date", ""))
        if d in daily_buckets:
            daily_buckets[d] += _to_decimal(b.get("total", 0))
    daily_spend = [float(daily_buckets[d]) for d in dates]

    # Top suppliers — from outstanding + posted-this-FY bill totals
    by_supplier: dict[str, Decimal] = {}
    for b in open_bills:
        if not (fy_from <= str(b.get("bill_date", "")) <= fy_to):
            continue
        cid = b.get("contact_id") or ""
        if cid:
            by_supplier[cid] = by_supplier.get(cid, Decimal("0")) + _to_decimal(b.get("total", 0))
    top_suppliers = [
        {"contact_id": cid, "name": cmap.get(cid, "—"), "total": tot}
        for cid, tot in sorted(by_supplier.items(), key=lambda x: x[1], reverse=True)[:5]
    ]

    # AP aging buckets
    def _bucket_days(b: dict) -> int:
        dd = b.get("due_date")
        if not dd:
            return 0
        try:
            return (today - date.fromisoformat(str(dd))).days
        except Exception:
            return 0

    aging = {"current": Decimal("0"), "d_1_30": Decimal("0"),
             "d_31_60": Decimal("0"), "d_61_90": Decimal("0"),
             "d_90_plus": Decimal("0")}
    for b in outstanding:
        owed = _to_decimal(b.get("total", 0)) - _to_decimal(b.get("amount_paid", 0))
        days = _bucket_days(b)
        if days <= 0:
            aging["current"] += owed
        elif days <= 30:
            aging["d_1_30"] += owed
        elif days <= 60:
            aging["d_31_60"] += owed
        elif days <= 90:
            aging["d_61_90"] += owed
        else:
            aging["d_90_plus"] += owed

    ctx = {
        "request": request,
        "today_iso": today.isoformat(),
        "due_soon_cutoff_iso": in_7.isoformat(),
        "month_start": month_start,
        "month_end": month_end,
        "fy_from": fy_from,
        "fy_to": fy_to,
        "draft_count": len(draft_bills),
        "draft_total": draft_total,
        "outstanding_count": len(outstanding),
        "outstanding_total": outstanding_total,
        "due_soon_count": len(due_soon),
        "due_soon_total": due_soon_total,
        "overdue_count": len(overdue),
        "overdue_total": overdue_total,
        "spend_mtd": spend_mtd,
        "spend_ytd": spend_ytd,
        "daily_spend": daily_spend,
        "daily_dates": dates,
        "top_suppliers": top_suppliers,
        "recent_bills": recent_bills,
        "recent_expenses": recent_expenses,
        "recent_pos": recent_pos,
        "recent_payments_out": recent_payments_out[:10],
        "contacts_by_id": cmap,
        "aging": aging,
    }
    return _TEMPLATES.TemplateResponse(request, "overviews/expenses.html", ctx)


# ─────────────────────────────────────────────────────────────────────────
# /inventory/overview
# ─────────────────────────────────────────────────────────────────────────


@router.get("/inventory/overview", response_class=HTMLResponse, response_model=None)
async def inventory_overview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    fy_from, fy_to = _au_fy_range()
    month_start, month_end = _this_month_range()

    async with api_client(request) as client:
        (
            items_raw,
            recent_pos,
            recent_invoices,
            recent_bills,
        ) = await asyncio.gather(
            _fetch_items(client, "/api/v1/items", {"page": 1, "page_size": 500}),
            _fetch_items(client, "/api/v1/purchase_orders", {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/invoices", {"page": 1, "page_size": 50}),
            _fetch_items(client, "/api/v1/bills", {"page": 1, "page_size": 50}),
        )

    # Classify items by kind (saebooks uses ItemKind enum: PRODUCT / SERVICE)
    products = [i for i in items_raw if (i.get("item_type") or i.get("kind") or "").upper() == "PRODUCT"]
    services = [i for i in items_raw if (i.get("item_type") or i.get("kind") or "").upper() == "SERVICE"]
    active = [i for i in items_raw if i.get("is_active", True) is not False]

    # Low-stock flag: products with on_hand <= reorder_threshold (if tracked).
    low_stock: list[dict] = []
    for i in products:
        thresh = i.get("reorder_threshold")
        on_hand = i.get("on_hand")
        if thresh is not None and on_hand is not None:
            try:
                if Decimal(str(on_hand)) <= Decimal(str(thresh)):
                    low_stock.append(i)
            except Exception:
                pass

    # Top sold items — count line refs across recent invoices
    sold_counts: dict[str, int] = {}
    sold_revenue: dict[str, Decimal] = {}
    for inv in recent_invoices:
        for line in (inv.get("lines") or []):
            iid = line.get("item_id")
            if iid:
                sold_counts[iid] = sold_counts.get(iid, 0) + 1
                sold_revenue[iid] = sold_revenue.get(iid, Decimal("0")) + _to_decimal(line.get("line_total", 0))

    items_by_id = {i.get("id"): i for i in items_raw if i.get("id")}
    top_sold = []
    for iid, cnt in sorted(sold_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
        it = items_by_id.get(iid, {})
        top_sold.append({
            "id": iid,
            "name": it.get("name", "—"),
            "sku": it.get("sku", ""),
            "count": cnt,
            "revenue": sold_revenue.get(iid, Decimal("0")),
        })

    ctx = {
        "request": request,
        "fy_from": fy_from,
        "fy_to": fy_to,
        "month_start": month_start,
        "month_end": month_end,
        "items_count": len(items_raw),
        "products_count": len(products),
        "services_count": len(services),
        "active_count": len(active),
        "low_stock": low_stock,
        "low_stock_count": len(low_stock),
        "top_sold": top_sold,
        "recent_pos": recent_pos,
        "recent_items": items_raw[:10],
    }
    return _TEMPLATES.TemplateResponse(request, "overviews/inventory.html", ctx)


# ─────────────────────────────────────────────────────────────────────────
# /gst/overview
# ─────────────────────────────────────────────────────────────────────────


@router.get("/gst/overview", response_class=HTMLResponse, response_model=None)
async def gst_overview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()
    fy_from, fy_to = _au_fy_range()

    # BAS quarter — Q1: Jul-Sep, Q2: Oct-Dec, Q3: Jan-Mar, Q4: Apr-Jun
    month = today.month
    year = today.year
    if month in (7, 8, 9):
        q_start, q_end, q_label = date(year, 7, 1), date(year, 9, 30), "Q1"
    elif month in (10, 11, 12):
        q_start, q_end, q_label = date(year, 10, 1), date(year, 12, 31), "Q2"
    elif month in (1, 2, 3):
        q_start, q_end, q_label = date(year, 1, 1), date(year, 3, 31), "Q3"
    else:
        q_start, q_end, q_label = date(year, 4, 1), date(year, 6, 30), "Q4"

    async with api_client(request) as client:
        (
            ytd_raw,
            tax_codes_raw,
            invoices_qtr,
            bills_qtr,
        ) = await asyncio.gather(
            _fetch_json(client, "/api/v1/reports/ytd_turnover"),
            _fetch_items(client, "/api/v1/tax_codes", {"page": 1, "page_size": 100}),
            _fetch_items(client, "/api/v1/invoices",
                         {"status": "POSTED", "page": 1, "page_size": 500,
                          "from_date": q_start.isoformat(),
                          "to_date": q_end.isoformat()}),
            _fetch_items(client, "/api/v1/bills",
                         {"status": "POSTED", "page": 1, "page_size": 500,
                          "from_date": q_start.isoformat(),
                          "to_date": q_end.isoformat()}),
        )

    ytd_turnover = _to_decimal(ytd_raw.get("ytd_turnover", 0))
    threshold = _to_decimal(ytd_raw.get("threshold", 75000))
    pct = float(ytd_turnover / threshold * 100) if threshold else 0.0
    threshold_crossed = bool(ytd_raw.get("threshold_crossed", False))
    threshold_approaching = bool(ytd_raw.get("threshold_approaching", False))

    # GST collected (output) vs paid (input) — best-effort from line tax_amount
    gst_out = Decimal("0")  # collected on sales
    gst_in = Decimal("0")   # paid on purchases
    sales_excl = Decimal("0")
    purchases_excl = Decimal("0")
    for inv in invoices_qtr:
        sales_excl += _to_decimal(inv.get("subtotal", 0) or inv.get("total_excl_tax", 0) or 0)
        gst_out += _to_decimal(inv.get("tax_total", 0) or inv.get("total_tax", 0) or 0)
    for b in bills_qtr:
        purchases_excl += _to_decimal(b.get("subtotal", 0) or b.get("total_excl_tax", 0) or 0)
        gst_in += _to_decimal(b.get("tax_total", 0) or b.get("total_tax", 0) or 0)

    net_gst = gst_out - gst_in

    # BAS due date — quarter end + 28 days
    bas_due = q_end + timedelta(days=28)
    days_to_bas = (bas_due - today).days

    ctx = {
        "request": request,
        "fy_from": fy_from,
        "fy_to": fy_to,
        "q_start": q_start.isoformat(),
        "q_end": q_end.isoformat(),
        "q_label": q_label,
        "bas_due": bas_due.isoformat(),
        "days_to_bas": days_to_bas,
        "ytd_turnover": ytd_turnover,
        "threshold": threshold,
        "pct": min(pct, 100.0),
        "threshold_crossed": threshold_crossed,
        "threshold_approaching": threshold_approaching,
        "tax_codes": tax_codes_raw,
        "tax_codes_count": len(tax_codes_raw),
        "sales_excl": sales_excl,
        "purchases_excl": purchases_excl,
        "gst_out": gst_out,
        "gst_in": gst_in,
        "net_gst": net_gst,
        "today_iso": today.isoformat(),
    }
    return _TEMPLATES.TemplateResponse(request, "overviews/gst.html", ctx)
