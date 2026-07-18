"""Group overview — multi-entity consolidated dashboard (M2 enterprise view).

GET /group — one row per active company: month-to-date revenue and net
profit plus AR/AP outstanding, fanned out per company with an explicit
``X-Company-Id`` header override per request. Per-currency subtotals only
(no FX translation — a consolidated single-currency view needs an engine
consolidation endpoint, which does not exist yet; SPEC-NEEDED).

Gating: the ``multi_company`` license flag (same source companies.py
uses). Not entitled → upsell panel, mirroring the companies-list wording.
A single-company tenant sees a hint instead of a one-row table.

Degrade: the companies/license fetch failing degrades the whole panel;
one company's report fetches failing degrades that ROW only.
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Fan-out ceiling — 3 report calls per company; beyond this the page notes
# the remainder rather than silently hammering the engine.
_MAX_COMPANIES = 20


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _sum_section_lines(sections: dict) -> Decimal:
    total = Decimal("0")
    for lines in (sections or {}).values():
        if isinstance(lines, list):
            total += sum(
                _to_decimal(l.get("amount", 0)) for l in lines if isinstance(l, dict)
            )
    return total


async def _company_row(client, company: dict) -> dict:
    """Fetch one company's KPI set. Returns a row dict; on any fetch
    failure the row comes back with ``degraded=True`` (never raises)."""
    cid = str(company.get("id", ""))
    headers = {"X-Company-Id": cid}
    month_start = date.today().replace(day=1).isoformat()
    today = date.today().isoformat()

    row = {
        "id": cid,
        "name": company.get("trading_name")
        or company.get("name")
        or company.get("legal_name")
        or cid,
        "currency": (company.get("currency") or "AUD").upper(),
        "revenue_mtd": Decimal("0"),
        "net_profit_mtd": Decimal("0"),
        "ar_total": Decimal("0"),
        "ap_total": Decimal("0"),
        "degraded": False,
    }
    try:
        pl_resp, ar_resp, ap_resp = await asyncio.gather(
            client.get(
                "/api/v1/reports/profit_loss",
                params={"from_date": month_start, "to_date": today},
                headers=headers,
            ),
            client.get("/api/v1/reports/aged_receivables", headers=headers),
            client.get("/api/v1/reports/aged_payables", headers=headers),
        )
    except Exception:
        # Raw transport errors surface here (api_client only wraps them
        # into ModuleUnavailable on context-manager exit); either way the
        # row degrades alone and the siblings keep rendering.
        row["degraded"] = True
        return row

    if pl_resp.is_success:
        pl = pl_resp.json()
        row["revenue_mtd"] = _sum_section_lines(pl.get("income", {}))
        row["net_profit_mtd"] = _to_decimal(pl.get("net_profit", 0))
    else:
        row["degraded"] = True
    if ar_resp.is_success:
        row["ar_total"] = _to_decimal(
            (ar_resp.json().get("totals", {}) or {}).get("total", 0)
        )
    else:
        row["degraded"] = True
    if ap_resp.is_success:
        row["ap_total"] = _to_decimal(
            (ap_resp.json().get("totals", {}) or {}).get("total", 0)
        )
    else:
        row["degraded"] = True
    return row


@router.get("/group", response_class=HTMLResponse, response_model=None)
async def group_overview(request: Request) -> HTMLResponse | RedirectResponse:
    """Consolidated per-company KPI table across the tenant's companies."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    degraded = False
    multi_company_enabled = False
    companies: list[dict] = []
    rows: list[dict] = []
    error: str | None = None
    truncated = 0

    try:
        async with api_client(request) as client:
            comp_resp, lic_resp = await asyncio.gather(
                client.get("/api/v1/companies", params={"limit": 100, "offset": 0}),
                client.get("/api/v1/license"),
            )
            if comp_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if comp_resp.is_success:
                companies = [
                    c
                    for c in comp_resp.json().get("items", [])
                    if not c.get("archived_at")
                ]
            else:
                error = _("The company list could not be loaded (HTTP %(code)s).") % {
                    "code": comp_resp.status_code
                }
            if lic_resp.is_success:
                flags = lic_resp.json().get("flags", {})
                multi_company_enabled = bool(flags.get("multi_company", False))

            if multi_company_enabled and companies and error is None:
                fanout = companies[:_MAX_COMPANIES]
                truncated = max(len(companies) - _MAX_COMPANIES, 0)
                rows = list(
                    await asyncio.gather(
                        *(_company_row(client, c) for c in fanout)
                    )
                )
    except ModuleUnavailable:
        degraded = True

    # Per-currency subtotals over the healthy rows only.
    subtotals: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        if r["degraded"]:
            continue
        cur = subtotals.setdefault(
            r["currency"],
            {
                "revenue_mtd": Decimal("0"),
                "net_profit_mtd": Decimal("0"),
                "ar_total": Decimal("0"),
                "ap_total": Decimal("0"),
            },
        )
        for k in cur:
            cur[k] += r[k]

    return _TEMPLATES.TemplateResponse(
        request,
        "group/overview.html",
        {
            "degraded": degraded,
            "error": error,
            "multi_company_enabled": multi_company_enabled,
            "companies_count": len(companies),
            "rows": rows,
            "subtotals": subtotals,
            "truncated": truncated,
            "month_start": date.today().replace(day=1).isoformat(),
            "today": date.today().isoformat(),
        },
    )
