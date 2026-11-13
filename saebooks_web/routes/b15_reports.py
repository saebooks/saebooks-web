"""Two report screens the engine already supported (b15, 2026-11-13 sweep).

* ``GET /reports/cash-forecast`` (gap P2-5) — the cash flow forecast the
  engine works out from open invoices, open bills and recurring invoices
  (``GET /api/v1/reports/cashflow_forecast``) had no screen. Shows the
  opening bank balance, money expected in and out, a week-by-week running
  balance (low weeks highlighted) and every expected item.
* ``GET /reports/aged-detail`` (gap P2-6) — aged receivables or payables
  with every open invoice or bill listed under its contact, aged by due
  date or by invoice date (``GET /api/v1/reports/aged_detail``).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)

HORIZONS = (30, 60, 90, 180)


@router.get("/reports/cash-forecast", response_class=HTMLResponse, response_model=None)
async def cash_forecast(request: Request, days: int = 90) -> HTMLResponse | RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    days = days if days in HORIZONS else 90
    report: dict = {}
    error = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get(
                "/api/v1/reports/cashflow_forecast", params={"horizon_days": days}
            )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = _("The forecast could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True
    weeks = report.get("weeks") or []
    lowest = min((float(w.get("running_balance") or 0) for w in weeks), default=None)
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/cash_forecast.html",
        {
            "report": report,
            "weeks": weeks,
            "items": report.get("items") or [],
            "lowest": lowest,
            "days": days,
            "horizons": HORIZONS,
            "error": error,
            "degraded": degraded,
        },
    )


@router.get("/reports/aged-detail", response_class=HTMLResponse, response_model=None)
async def aged_detail(
    request: Request,
    side: str = "receivables",
    as_at: str | None = None,
    age_by: str = "due",
) -> HTMLResponse | RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    side = side if side in ("receivables", "payables") else "receivables"
    age_by = age_by if age_by in ("due", "issue") else "due"
    params: dict[str, str] = {"side": side, "age_by": age_by}
    if as_at:
        params["as_at"] = as_at
    report: dict = {}
    error = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/reports/aged_detail", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            report = resp.json()
        else:
            error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/aged_detail.html",
        {
            "report": report,
            "side": side,
            "age_by": age_by,
            "as_at": report.get("as_at") or as_at or "",
            "error": error,
            "degraded": degraded,
            "doc_base": "/invoices/" if side == "receivables" else "/bills/",
        },
    )
