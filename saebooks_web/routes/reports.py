"""Reports HTML views — Lane D cycle 29/32/41/PSI-2.

GET /reports/fx-revaluation        — FX revaluation table (as_of_date param)

GET /reports/aged-receivables       — AR ageing table (as_of_date param)
GET /reports/aged-payables          — AP ageing table (as_of_date param)
GET /reports/profit-loss            — P&L by account type (from/to date range)
GET /reports/balance-sheet          — Balance sheet (as_of_date param)
GET /reports/bas-summary            — BAS summary (from/to date range)
GET /reports/cashflow               — Cashflow statement (from/to date range)
GET /reports/depreciation-schedule  — Depreciation schedule (as_of_date, method)
GET /reports/fx-revaluation         — FX revaluation report (as_of_date)
GET /reports/trial-balance          — Trial balance (as_of_date, include_zero_balance)
GET /reports/budget-vs-actual       — Budget vs actual (year, month)
GET /reports/pl-by-segment          — P&L by segment (from_date, to_date, segment_type)
GET /reports/revenue-by-customer    — Revenue by customer (from_date, to_date)

All routes are HTMX-aware: when the request carries HX-Request: true the
route renders only the ``_table`` partial (no base.html wrapper).

Date defaults:
  - as_of_date: today
  - from_date: first day of current month
  - to_date: today
  - BAS from/to: first day of current quarter to today

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import calendar

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _today() -> str:
    return date.today().isoformat()


def _month_start() -> str:
    today = date.today()
    return today.replace(day=1).isoformat()


def _quarter_start() -> str:
    """Return YYYY-MM-01 for the first month of the current calendar quarter."""
    today = date.today()
    # Q1: Jan-Mar → 1, Q2: Apr-Jun → 4, Q3: Jul-Sep → 7, Q4: Oct-Dec → 10
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    return today.replace(month=quarter_month, day=1).isoformat()


def _month_end() -> str:
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=last_day).isoformat()


def _current_year() -> int:
    return date.today().year


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# ---------------------------------------------------------------------------
# GET /reports  — index card grid
# ---------------------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse, response_model=None)
async def reports_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Reports index — card grid linking to all implemented report pages."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(request, "reports/index.html", {})


# ---------------------------------------------------------------------------
# GET /reports/aged-receivables
# ---------------------------------------------------------------------------


@router.get("/reports/aged-receivables", response_class=HTMLResponse, response_model=None)
async def aged_receivables(
    request: Request,
    as_of_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Aged receivables report — per-contact AR ageing table."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/aged_receivables",
            params={"as_of_date": as_of},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
        "report_title": "Aged Receivables",
        "report_url": "/reports/aged-receivables",
    }

    template = (
        "reports/_aged_receivables_table.html"
        if _is_htmx(request)
        else "reports/aged_receivables.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/aged-payables
# ---------------------------------------------------------------------------


@router.get("/reports/aged-payables", response_class=HTMLResponse, response_model=None)
async def aged_payables(
    request: Request,
    as_of_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Aged payables report — per-contact AP ageing table."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/aged_payables",
            params={"as_of_date": as_of},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
        "report_title": "Aged Payables",
        "report_url": "/reports/aged-payables",
    }

    template = (
        "reports/_aged_payables_table.html"
        if _is_htmx(request)
        else "reports/aged_payables.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/profit-loss
# ---------------------------------------------------------------------------


@router.get("/reports/profit-loss", response_class=HTMLResponse, response_model=None)
async def profit_loss(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    include_draft: bool = False,
) -> HTMLResponse | RedirectResponse:
    """Profit & Loss report — income/expense sections and net profit."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    from_ = from_date or _month_start()
    to_ = to_date or _today()
    report: dict = {}
    error: str | None = None
    gst: dict = {}

    async with api_client(request) as client:
        pl_resp, ytd_resp = await asyncio.gather(
            client.get(
                "/api/v1/reports/profit_loss",
                params={"from_date": from_, "to_date": to_, "include_draft": str(include_draft).lower()},
            ),
            client.get("/api/v1/reports/ytd_turnover"),
        )
        if pl_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if pl_resp.is_success:
            report = pl_resp.json()
        else:
            error = f"API error: HTTP {pl_resp.status_code}"
        if ytd_resp.is_success:
            ytd_data = ytd_resp.json()
            ytd = float(ytd_data.get("ytd_turnover", 0))
            threshold = float(ytd_data.get("threshold", 75000))
            gst = {
                "ytd_turnover": ytd,
                "threshold": threshold,
                "threshold_crossed": bool(ytd_data.get("threshold_crossed", False)),
                "threshold_approaching": bool(ytd_data.get("threshold_approaching", False)),
                "pct": min(ytd / threshold * 100 if threshold else 0.0, 100.0),
                "fy_start": ytd_data.get("fy_start", ""),
                "fy_end": ytd_data.get("fy_end", ""),
            }

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "include_draft": include_draft,
        "error": error,
        "gst": gst,
    }

    template = (
        "reports/_profit_loss_table.html"
        if _is_htmx(request)
        else "reports/profit_loss.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/balance-sheet
# ---------------------------------------------------------------------------


@router.get("/reports/balance-sheet", response_class=HTMLResponse, response_model=None)
async def balance_sheet(
    request: Request,
    as_of_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Balance sheet report — assets, liabilities, equity sections."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/balance_sheet",
            params={"as_of_date": as_of},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
    }

    template = (
        "reports/_balance_sheet_table.html"
        if _is_htmx(request)
        else "reports/balance_sheet.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/bas-summary
# ---------------------------------------------------------------------------


@router.get("/reports/bas-summary", response_class=HTMLResponse, response_model=None)
async def bas_summary(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """BAS summary report — G1, G3, G11, 1A, 1B, Net GST, remit/refund."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    from_ = from_date or _quarter_start()
    to_ = to_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        # Fetch company GST registration date so we can split G1 for mid-quarter
        # registrations (ATO compliance — HOBB-3).
        gst_effective_date: str | None = None
        clist_resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if clist_resp.is_success:
            companies = clist_resp.json().get("items", [])
            if companies:
                gst_effective_date = companies[0].get("gst_effective_date") or None

        params: dict = {"from_date": from_, "to_date": to_}
        if gst_effective_date:
            params["registration_effective_date"] = gst_effective_date

        resp = await client.get("/api/v1/reports/bas_summary", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "error": error,
    }

    template = (
        "reports/_bas_summary_table.html"
        if _is_htmx(request)
        else "reports/bas_summary.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/cashflow
# ---------------------------------------------------------------------------


@router.get("/reports/cashflow", response_class=HTMLResponse, response_model=None)
async def cashflow(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Cashflow statement — operating / investing / financing waterfall."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    from_ = from_date or _month_start()
    to_ = to_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/cashflow",
            params={"from_date": from_, "to_date": to_},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "error": error,
    }

    template = (
        "reports/_cashflow_table.html"
        if _is_htmx(request)
        else "reports/cashflow.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/depreciation-schedule
# ---------------------------------------------------------------------------


@router.get("/reports/depreciation-schedule", response_class=HTMLResponse, response_model=None)
async def depreciation_schedule(
    request: Request,
    as_of_date: str | None = None,
    method: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Depreciation schedule — per-asset table with method, cost, book value."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None

    params: dict = {"as_of_date": as_of}
    # Omit method param when "all" or not supplied
    if method and method != "all":
        params["method"] = method

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/depreciation_schedule",
            params=params,
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "method": method or "all",
        "error": error,
    }

    template = (
        "reports/_depreciation_schedule_table.html"
        if _is_htmx(request)
        else "reports/depreciation_schedule.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/fx-revaluation
# ---------------------------------------------------------------------------


@router.get("/reports/fx-revaluation", response_class=HTMLResponse, response_model=None)
async def fx_revaluation(
    request: Request,
    as_of_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """FX revaluation report — POSTED invoices/bills in non-base currency."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/fx_revaluation",
            params={"as_of_date": as_of},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
    }

    template = (
        "reports/_fx_revaluation_table.html"
        if _is_htmx(request)
        else "reports/fx_revaluation.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/trial-balance
# ---------------------------------------------------------------------------


@router.get("/reports/trial-balance", response_class=HTMLResponse, response_model=None)
async def trial_balance(
    request: Request,
    as_of_date: str | None = None,
    include_zero_balance: bool = False,
) -> HTMLResponse | RedirectResponse:
    """Trial balance report — debits/credits/balance per account with balanced indicator."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/trial_balance",
            params={
                "as_of_date": as_of,
                "include_zero_balance": str(include_zero_balance).lower(),
            },
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "include_zero_balance": include_zero_balance,
        "error": error,
    }

    template = (
        "reports/_trial_balance_table.html"
        if _is_htmx(request)
        else "reports/trial_balance.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/budget-vs-actual
# ---------------------------------------------------------------------------


@router.get("/reports/budget-vs-actual", response_class=HTMLResponse, response_model=None)
async def budget_vs_actual(
    request: Request,
    year: int | None = None,
    month: int | None = None,
) -> HTMLResponse | RedirectResponse:
    """Budget vs actual report — budget/actual/variance per account for a period."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    yr = year if year is not None else _current_year()
    report: dict = {}
    error: str | None = None

    params: dict = {"year": yr}
    if month is not None:
        params["month"] = month

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/budget_vs_actual",
            params=params,
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "year": yr,
        "month": month,
        "current_year": _current_year(),
        "error": error,
    }

    template = (
        "reports/_budget_vs_actual_table.html"
        if _is_htmx(request)
        else "reports/budget_vs_actual.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/pl-by-segment
# ---------------------------------------------------------------------------


@router.get("/reports/pl-by-segment", response_class=HTMLResponse, response_model=None)
async def pl_by_segment(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    segment_type: str = "project",
) -> HTMLResponse | RedirectResponse:
    """P&L by segment report — net profit per project/department/cost-centre."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    from_ = from_date or _month_start()
    to_ = to_date or _month_end()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/pl_by_segment",
            params={"from_date": from_, "to_date": to_, "segment_type": segment_type},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "segment_type": segment_type,
        "error": error,
    }

    template = (
        "reports/_pl_by_segment_table.html"
        if _is_htmx(request)
        else "reports/pl_by_segment.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# GET /reports/revenue-by-customer — gap PSI-2
# ---------------------------------------------------------------------------


@router.get("/reports/revenue-by-customer", response_class=HTMLResponse, response_model=None)
async def revenue_by_customer(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Revenue by customer report — invoiced revenue (ex-GST) per contact."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    from_ = from_date or _month_start()
    to_ = to_date or _month_end()
    report: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/revenue_by_customer",
            params={"from_date": from_, "to_date": to_},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "error": error,
    }

    template = (
        "reports/_revenue_by_customer_table.html"
        if _is_htmx(request)
        else "reports/revenue_by_customer.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Period-end / year-end close — GUI-rebuild (period-end pages)
# ---------------------------------------------------------------------------


def _last_fy_end() -> str:
    """Default AU financial-year-end (30 June, most recent on/before today)."""
    today = date.today()
    year = today.year if today.month >= 7 else today.year - 1
    return date(year, 6, 30).isoformat()


async def _fetch_equity_accounts(client) -> list[dict]:
    """EQUITY accounts for the retained-earnings picker (active only)."""
    resp = await client.get(
        "/api/v1/accounts",
        params={
            "account_type": "EQUITY",
            "include_archived": False,
            "limit": 200,
            "offset": 0,
        },
    )
    if resp.is_success:
        return resp.json().get("items", [])
    return []


def _detail_or_status(resp) -> str:
    """Best-effort error detail string from a non-2xx API response."""
    try:
        return str(resp.json().get("detail", "")) or f"HTTP {resp.status_code}"
    except Exception:
        return f"HTTP {resp.status_code}"


@router.get("/reports/period-end", response_class=HTMLResponse, response_model=None)
async def period_end(
    request: Request,
    through_date: str | None = None,
    retained_earnings_account_id: str | None = None,
    from_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Period-end / year-end close — preview the retained-earnings roll.

    Reads ``/api/v1/period-close/preview``; the actual close is a separate
    POST so the destructive step is always an explicit confirm.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    through = through_date or _last_fy_end()
    accounts: list[dict] = []
    preview: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        accounts = await _fetch_equity_accounts(client)
        # Auto-select a retained-earnings account when none chosen yet.
        if not retained_earnings_account_id:
            for a in accounts:
                nm = (a.get("name") or "").lower()
                if "retain" in nm or "earnings" in nm:
                    retained_earnings_account_id = a.get("id")
                    break

        if retained_earnings_account_id:
            params: dict = {
                "through_date": through,
                "retained_earnings_account_id": retained_earnings_account_id,
            }
            if from_date:
                params["from_date"] = from_date
            resp = await client.get("/api/v1/period-close/preview", params=params)
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                preview = resp.json()
            else:
                error = f"Preview failed: {_detail_or_status(resp)}"

    ctx = {
        "accounts": accounts,
        "through_date": through,
        "from_date": from_date or "",
        "retained_earnings_account_id": retained_earnings_account_id or "",
        "preview": preview,
        "error": error,
        "closed": None,
        "journal_entry_id": None,
    }
    template = (
        "reports/_period_end_preview.html"
        if _is_htmx(request)
        else "reports/period_end.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.post("/reports/period-end/close", response_class=HTMLResponse, response_model=None)
async def period_end_close(request: Request) -> HTMLResponse | RedirectResponse:
    """Execute the year-end close — posts the RE journal and locks the period.

    HTMX-posted from the preview; returns the preview partial showing either a
    success banner (with a link to the posted journal) or the API error so a
    period-lock can be re-tried with an override reason.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    through = (form.get("through_date") or _last_fy_end())
    re_account = (form.get("retained_earnings_account_id") or "").strip() or None
    from_date = (form.get("from_date") or "").strip() or None
    override_reason = (form.get("override_reason") or "").strip() or None

    accounts: list[dict] = []
    preview: dict = {}
    error: str | None = None
    closed: bool | None = None
    journal_entry_id: str | None = None

    async with api_client(request) as client:
        accounts = await _fetch_equity_accounts(client)
        if not re_account:
            error = "Select a retained-earnings account before closing."
        else:
            body: dict = {
                "through_date": through,
                "retained_earnings_account_id": re_account,
            }
            if from_date:
                body["from_date"] = from_date
            if override_reason:
                body["override_reason"] = override_reason
            resp = await client.post("/api/v1/period-close/close-year", json=body)
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                data = resp.json()
                closed = data.get("closed", False)
                journal_entry_id = data.get("journal_entry_id")
            else:
                error = f"Close failed: {_detail_or_status(resp)}"
                # Re-show the preview so the operator sees what would post.
                params = {
                    "through_date": through,
                    "retained_earnings_account_id": re_account,
                }
                if from_date:
                    params["from_date"] = from_date
                pv = await client.get("/api/v1/period-close/preview", params=params)
                if pv.is_success:
                    preview = pv.json()

    ctx = {
        "accounts": accounts,
        "through_date": through,
        "from_date": from_date or "",
        "retained_earnings_account_id": re_account or "",
        "preview": preview,
        "error": error,
        "closed": closed,
        "journal_entry_id": journal_entry_id,
    }
    return _TEMPLATES.TemplateResponse(request, "reports/_period_end_preview.html", ctx)


# ---------------------------------------------------------------------------
# BAS / PAYG review worksheet — GUI-rebuild (BAS/PAYG review screens)
# ---------------------------------------------------------------------------


@router.get("/reports/bas-payg", response_class=HTMLResponse, response_model=None)
async def bas_payg_review(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """BAS/PAYG review worksheet for a period.

    Consolidates the GST labels (G1..G11, 1A, 1B, net GST) from
    ``/api/v1/reports/bas_summary`` with PAYG withholding (W1 total gross,
    W2 tax withheld) derived from finalised pay-runs whose payment date falls
    in the period, and totals the amount payable to the ATO.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    from_ = from_date or _quarter_start()
    to_ = to_date or _today()
    report: dict = {}
    payg = {"w1_total_gross": 0.0, "w2_tax_withheld": 0.0, "pay_run_count": 0}
    error: str | None = None

    async with api_client(request) as client:
        # --- GST labels (BAS summary, with mid-period registration split) ---
        gst_effective_date: str | None = None
        clist = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if clist.is_success:
            items = clist.json().get("items", [])
            if items:
                gst_effective_date = items[0].get("gst_effective_date") or None

        params: dict = {"from_date": from_, "to_date": to_}
        if gst_effective_date:
            params["registration_effective_date"] = gst_effective_date
        resp = await client.get("/api/v1/reports/bas_summary", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = f"GST API error: {_detail_or_status(resp)}"

        # --- PAYG withholding from pay-runs in the period ---
        pr = await client.get("/api/v1/pay-runs", params={"limit": 500, "offset": 0})
        if pr.is_success:
            w1 = 0.0
            w2 = 0.0
            count = 0
            for run in pr.json().get("items", []):
                pay_date = run.get("payment_date") or run.get("period_end")
                if not (pay_date and from_ <= pay_date <= to_):
                    continue
                count += 1
                for line in run.get("lines", []):
                    try:
                        w1 += float(line.get("gross") or 0)
                        w2 += float(line.get("tax") or 0)
                    except (TypeError, ValueError):
                        continue
            payg = {
                "w1_total_gross": round(w1, 2),
                "w2_tax_withheld": round(w2, 2),
                "pay_run_count": count,
            }

    # Amount payable to ATO = net GST (1A - 1B) + PAYG withheld (W2).
    # PAYG instalment (5A/T7) is not modelled in the engine — entered on the
    # ATO BAS directly; surfaced as a note on the worksheet.
    try:
        net_gst = float(report.get("net_gst", 0) or 0)
    except (TypeError, ValueError):
        net_gst = 0.0
    amount_payable = round(net_gst + payg["w2_tax_withheld"], 2)

    ctx = {
        "report": report,
        "payg": payg,
        "from_date": from_,
        "to_date": to_,
        "net_gst": net_gst,
        "amount_payable": amount_payable,
        "error": error,
    }
    template = (
        "reports/_bas_payg_table.html"
        if _is_htmx(request)
        else "reports/bas_payg.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)
