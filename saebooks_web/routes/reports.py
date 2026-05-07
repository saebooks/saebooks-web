"""Reports HTML views — Lane D.

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
# GET /reports/revenue-by-customer
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
