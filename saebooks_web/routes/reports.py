"""Reports HTML views — Lane D cycle 29.

GET /reports/aged-receivables       — AR ageing table (as_of_date param)
GET /reports/aged-payables          — AP ageing table (as_of_date param)
GET /reports/profit-loss            — P&L by account type (from/to date range)
GET /reports/balance-sheet          — Balance sheet (as_of_date param)
GET /reports/bas-summary            — BAS summary (from/to date range)
GET /reports/cashflow               — Cashflow statement (from/to date range)
GET /reports/depreciation-schedule  — Depreciation schedule (as_of_date, method)

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

from datetime import date
from pathlib import Path

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


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


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

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/reports/profit_loss",
            params={"from_date": from_, "to_date": to_, "include_draft": str(include_draft).lower()},
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
        "include_draft": include_draft,
        "error": error,
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
        resp = await client.get(
            "/api/v1/reports/bas_summary",
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
