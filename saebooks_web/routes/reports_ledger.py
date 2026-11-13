"""Reports that agree with the ledger -- batch 7 screens.

GET /reports/general-ledger           -- every account's transactions (P1-26)
GET /reports/journal                  -- every posted entry with its lines
GET /reports/periods                  -- P&L / balance sheet by month,
                                         quarter or year (P1-29)
GET /reports/accountant-pack          -- year-end download page (P1-26)
GET /reports/data-export              -- backup and export page (P1-27)
GET /reports/data-export/download     -- streams the export ZIP

Downloads for the first four go through the shared ``/reports/export/{name}``
proxy in ``routes/reports.py``. The data export has its own download route
because it is for administrators only.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web import period as period_mod
from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable
from saebooks_web.routes.reports import (
    _is_htmx,
    _period_preset_options,
    _require_admin,
    _require_auth,
    _resolve_period_for_request,
)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_JOURNAL_PAGE_SIZE = 100


def _fy_defaults(fin_year_start_month: int) -> tuple[str, str]:
    """(start of this financial year, today) as ISO strings."""
    today = date.today()
    fy_start, _fy_end = period_mod.fy_bounds_containing(today, fin_year_start_month=fin_year_start_month)
    return fy_start.isoformat(), today.isoformat()


def _qs(**params: object) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v not in (None, "")})


# ---------------------------------------------------------------------------
# General ledger
# ---------------------------------------------------------------------------


@router.get("/reports/general-ledger", response_class=HTMLResponse, response_model=None)
async def general_ledger(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    preset: str | None = None,
    account_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    report: dict = {}
    accounts: list[dict] = []
    error: str | None = None
    degraded = False
    from_, to_, active_preset = from_date or "", to_date or "", "custom"
    try:
        async with api_client(request) as client:
            fy_month = await period_mod.fetch_fin_year_start_month(client)
            default_from, default_to = _fy_defaults(fy_month)
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=default_from, default_to=default_to,
            )
            params: dict[str, str] = {"from_date": from_, "to_date": to_}
            if account_id:
                params["account_id"] = account_id
            resp = await client.get("/api/v1/reports/general_ledger", params=params)
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                report = resp.json()
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
            if not _is_htmx(request):
                a_resp = await client.get("/api/v1/accounts", params={"limit": 1000, "offset": 0})
                if a_resp.is_success:
                    accounts = [a for a in a_resp.json().get("items", []) if not a.get("is_header")]
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "accounts": accounts,
        "account_id": account_id or "",
        "from_date": from_,
        "to_date": to_,
        "error": error,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
        "export_qs": _qs(from_date=from_, to_date=to_, account_id=account_id),
    }
    template = (
        "reports/_general_ledger_table.html" if _is_htmx(request) else "reports/general_ledger.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


@router.get("/reports/journal", response_class=HTMLResponse, response_model=None)
async def journal(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    preset: str | None = None,
    page: int = 1,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    page = max(page, 1)
    report: dict = {}
    error: str | None = None
    degraded = False
    from_, to_, active_preset = from_date or "", to_date or "", "custom"
    try:
        async with api_client(request) as client:
            fy_month = await period_mod.fetch_fin_year_start_month(client)
            default_from, default_to = _fy_defaults(fy_month)
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=default_from, default_to=default_to,
            )
            resp = await client.get(
                "/api/v1/reports/journal",
                params={
                    "from_date": from_,
                    "to_date": to_,
                    "limit": _JOURNAL_PAGE_SIZE,
                    "offset": (page - 1) * _JOURNAL_PAGE_SIZE,
                },
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                report = resp.json()
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    total = int(report.get("total", 0) or 0)
    pages = max((total + _JOURNAL_PAGE_SIZE - 1) // _JOURNAL_PAGE_SIZE, 1)
    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "page": page,
        "pages": pages,
        "total": total,
        "error": error,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
        "export_qs": _qs(from_date=from_, to_date=to_),
        "page_qs": lambda p: _qs(from_date=from_, to_date=to_, page=p),
    }
    template = "reports/_journal_table.html" if _is_htmx(request) else "reports/journal.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Period columns (P1-29)
# ---------------------------------------------------------------------------

_REPORTS = {"profit_loss", "balance_sheet"}
_PERIODS = {"month", "quarter", "year"}


@router.get("/reports/periods", response_class=HTMLResponse, response_model=None)
async def periods(
    request: Request,
    report: str = "profit_loss",
    period: str = "month",
    from_date: str | None = None,
    to_date: str | None = None,
    preset: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    which = report if report in _REPORTS else "profit_loss"
    kind = period if period in _PERIODS else "month"

    data: dict = {}
    error: str | None = None
    degraded = False
    from_, to_, active_preset = from_date or "", to_date or "", "custom"
    try:
        async with api_client(request) as client:
            fy_month = await period_mod.fetch_fin_year_start_month(client)
            default_from, default_to = _fy_defaults(fy_month)
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=default_from, default_to=default_to,
            )
            resp = await client.get(
                f"/api/v1/reports/{which}_columns",
                params={"from_date": from_, "to_date": to_, "period": kind},
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                data = resp.json()
            elif resp.status_code == 422:
                detail = resp.json().get("detail") if resp.headers.get("content-type", "").startswith("application/json") else None
                error = detail if isinstance(detail, str) else _("Choose a shorter range or a longer period.")
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    sections = (
        [("income", _("Income")), ("expense", _("Expenses"))]
        if which == "profit_loss"
        else [("asset", _("Assets")), ("liability", _("Liabilities")), ("equity", _("Equity"))]
    )
    ctx = {
        "data": data,
        "which": which,
        "kind": kind,
        "sections": sections,
        "from_date": from_,
        "to_date": to_,
        "error": error,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
        "export_qs": _qs(from_date=from_, to_date=to_, period=kind),
        "title": _("Profit & Loss") if which == "profit_loss" else _("Balance Sheet"),
    }
    template = "reports/_periods_table.html" if _is_htmx(request) else "reports/periods.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Accountant pack
# ---------------------------------------------------------------------------


@router.get("/reports/accountant-pack", response_class=HTMLResponse, response_model=None)
async def accountant_pack(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    degraded = False
    fy_month = 7
    try:
        async with api_client(request) as client:
            fy_month = await period_mod.fetch_fin_year_start_month(client)
    except ModuleUnavailable:
        degraded = True
    # Default to the last COMPLETED financial year -- that is what the
    # accountant asks for.
    this_fy_start, _this_fy_end = period_mod.fy_bounds_containing(date.today(), fin_year_start_month=fy_month)
    last_fy_end = this_fy_start - timedelta(days=1)
    last_fy_start, _ = period_mod.fy_bounds_containing(last_fy_end, fin_year_start_month=fy_month)
    from_ = from_date or last_fy_start.isoformat()
    to_ = to_date or last_fy_end.isoformat()
    ctx = {
        "from_date": from_,
        "to_date": to_,
        "degraded": degraded,
        "download_qs": _qs(from_date=from_, to_date=to_),
    }
    return _TEMPLATES.TemplateResponse(request, "reports/accountant_pack.html", ctx)


# ---------------------------------------------------------------------------
# Backup and export (P1-27)
# ---------------------------------------------------------------------------


@router.get("/reports/data-export", response_class=HTMLResponse, response_model=None)
async def data_export_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    ctx = {
        "is_admin": _require_admin(request),
        "flash": request.session.pop("flash", None),
    }
    return _TEMPLATES.TemplateResponse(request, "reports/data_export.html", ctx)


@router.get("/reports/data-export/download", response_model=None)
async def data_export_download(request: Request) -> Response | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        request.session["flash"] = _("Only an administrator can download the full export.")
        return RedirectResponse(url="/reports/data-export", status_code=303)
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/reports/data_export.zip", timeout=300.0)
    except ModuleUnavailable:
        request.session["flash"] = _("The export could not be built just now. Try again in a minute.")
        return RedirectResponse(url="/reports/data-export", status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code != 200:
        request.session["flash"] = _("The export could not be built (HTTP %(code)s).") % {"code": resp.status_code}
        return RedirectResponse(url="/reports/data-export", status_code=303)
    return Response(
        content=resp.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": resp.headers.get(
                "content-disposition", 'attachment; filename="company-export.zip"'
            ),
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )
