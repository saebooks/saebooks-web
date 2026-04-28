"""Dashboard home page — Lane D cycle 25.

GET /  → render the dashboard, computing AR/AP/cash tiles and recent activity
         from parallel API calls via asyncio.gather.

Tiles:
  AR at a glance  — draft invoices, open (POSTED, overdue computed in Python),
                    paid-this-month
  AP at a glance  — draft bills, due-within-7-days bills, paid-this-month bills
  Cash movement   — this month's payment IN total, OUT total, net
  Recent activity — last 5 items across invoices/bills/payments/journal_entries/
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

    overdue = [
        inv for inv in open_invoices
        if inv.get("due_date") and date.fromisoformat(str(inv["due_date"])) < today
    ]
    overdue_count = len(overdue)
    overdue_total = sum(_to_decimal(inv.get("total", 0)) for inv in overdue)

    paid_count = len(paid_invoices)
    paid_total = sum(_to_decimal(inv.get("total", 0)) for inv in paid_invoices)

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

    due_soon = [
        b for b in open_bills
        if b.get("due_date") and today <= date.fromisoformat(str(b["due_date"])) <= in_7
    ]
    due_soon_count = len(due_soon)
    due_soon_total = sum(_to_decimal(b.get("total", 0)) for b in due_soon)

    paid_count = len(paid_bills)
    paid_total = sum(_to_decimal(b.get("total", 0)) for b in paid_bills)

    return {
        "draft_count": draft_count,
        "draft_total": draft_total,
        "due_soon_count": due_soon_count,
        "due_soon_total": due_soon_total,
        "paid_count": paid_count,
        "paid_total": paid_total,
    }


def _cash_tile(payments: list[dict]) -> dict:
    """Compute cash-movement totals for the current month from payments list."""
    month_start, month_end = _this_month_range()

    in_total = Decimal("0")
    out_total = Decimal("0")

    for pmt in payments:
        pmt_date = pmt.get("payment_date", "")
        if not (pmt_date and month_start <= str(pmt_date) <= month_end):
            continue
        amount = _to_decimal(pmt.get("amount", 0))
        direction = pmt.get("direction", "")
        if direction == "INCOMING":
            in_total += amount
        elif direction == "OUTGOING":
            out_total += amount

    return {
        "in_total": in_total,
        "out_total": out_total,
        "net": in_total - out_total,
    }


def _gst_tile(ytd_data: dict) -> dict:
    """Extract GST turnover data from the ytd_turnover API response.

    Returns a safe dict regardless of whether the API call succeeded.
    threshold_crossed=False and ytd_turnover=0.0 are the safe defaults.
    """
    return {
        "ytd_turnover": _to_decimal(ytd_data.get("ytd_turnover", 0)),
        "threshold": _to_decimal(ytd_data.get("threshold", 75000)),
        "threshold_crossed": bool(ytd_data.get("threshold_crossed", False)),
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

    for item in invoices:
        tagged.append({"entity_type": "Invoice", "item": item,
                        "label": item.get("number") or item.get("id", ""),
                        "url": f"/invoices/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in bills:
        tagged.append({"entity_type": "Bill", "item": item,
                        "label": item.get("number") or item.get("id", ""),
                        "url": f"/bills/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in payments:
        tagged.append({"entity_type": "Payment", "item": item,
                        "label": item.get("number") or item.get("reference") or item.get("id", ""),
                        "url": f"/payments/{item.get('id', '')}",
                        "created_at": item.get("created_at", "")})

    for item in journal_entries:
        tagged.append({"entity_type": "Journal Entry", "item": item,
                        "label": item.get("number") or item.get("id", ""),
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

    month_start, month_end = _this_month_range()

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
                         {"page": 1, "page_size": 10}),
            _fetch_items(client, "/api/v1/contacts",
                         {"page": 1, "page_size": 10}),
            # GST turnover tile
            _fetch_json(client, "/api/v1/reports/ytd_turnover"),
        )

    # Handle 401 edge case — if all lists are empty and the token is gone,
    # we let the page render empty rather than soft-looping.  The user can
    # navigate away; a future middleware improvement would catch this globally.

    ar = _ar_tile(draft_invoices_raw, open_invoices_raw, paid_inv_raw)
    ap = _ap_tile(draft_bills_raw, open_bills_raw, paid_bills_raw)
    cash = _cash_tile(payments_raw)
    recent = _recent_activity(
        recent_invoices_raw,
        recent_bills_raw,
        recent_payments_raw,
        recent_je_raw,
        recent_contacts_raw,
    )
    gst = _gst_tile(ytd_turnover_raw)

    return _TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "ar": ar,
            "ap": ap,
            "cash": cash,
            "recent": recent,
            "gst": gst,
        },
    )
