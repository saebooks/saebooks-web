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
import calendar
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web import period
from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _require_admin(request: Request) -> bool:
    """True if the session is SAE staff or a tenant owner/admin (year-end close)."""
    role = request.session.get("user_role", "")
    return bool(request.session.get("is_sae_staff")) or role in ("owner", "admin")


def _last_fy_end(fin_year_start_month: int = 7) -> str:
    """End date (ISO string) of the most recently completed financial year.

    Named "_last_fy_end" (not "_au…") — derives from the company's actual
    ``fin_year_start_month`` via ``saebooks_web.period.fy_bounds_containing``
    rather than hardcoding 30 June. Defaults to the AU 1 Jul-30 Jun FY when
    no month is supplied (matches the historical behaviour for callers that
    haven't been updated to pass one).
    """
    today = date.today()
    fy_start, _fy_end = period.fy_bounds_containing(
        today, fin_year_start_month=fin_year_start_month
    )
    return (fy_start - timedelta(days=1)).isoformat()


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
# Period picker — shared preset resolution for from/to-date report routes.
#
# Precedence: (1) an explicit preset/from_date/to_date on THIS request wins;
# (2) otherwise the period persisted in the session from the last
# period-aware page (dashboard or another report) is replayed — presets are
# re-resolved against *today* so "This FY" stays correct as days roll over,
# a stored custom range is replayed verbatim; (3) otherwise the route's own
# hardcoded default (unchanged behaviour for a fresh session / no params).
# ---------------------------------------------------------------------------


def _period_preset_options() -> list[tuple[str, str]]:
    """Preset (value, label) pairs for the period-picker partial.

    Built inside a request-handling function (not at module import time) so
    ``_()`` resolves the current request's locale — see
    ``saebooks_web/i18n/__init__.py``'s module docstring on why gettext is
    call-time, not env-bound.
    """
    return [
        ("this_fy", _("This FY")),
        ("last_fy", _("Last FY")),
        ("calendar_ytd", _("Calendar year to date")),
        ("trailing_12", _("Trailing 12 months")),
        ("this_quarter", _("This quarter")),
    ]


async def _fetch_fin_year_start_month(client) -> int:
    """Fetch the active company's fin_year_start_month; 7 (AU default) on any failure."""
    return await period.fetch_fin_year_start_month(client)


async def _resolve_period_for_request(
    request: Request,
    client,
    preset: str | None,
    from_date: str | None,
    to_date: str | None,
    default_from: str,
    default_to: str,
) -> tuple[str, str, str]:
    """Resolve the effective (from_date, to_date, active_preset) for a
    period-aware report route, applying session persistence.

    Always writes the resolved period back to the session so the next
    period-aware page picks it up.
    """
    explicit = bool(preset or from_date or to_date)

    if explicit:
        if preset in period.PRESET_IDS:
            fin_year_start_month = await _fetch_fin_year_start_month(client)
            from_, to_, active = period.resolve_period(
                preset, fin_year_start_month=fin_year_start_month
            )
        else:
            from_ = from_date or default_from
            to_ = to_date or default_to
            active = "custom"
    else:
        sess_preset = request.session.get("period_preset")
        if sess_preset in period.PRESET_IDS:
            fin_year_start_month = await _fetch_fin_year_start_month(client)
            from_, to_, active = period.resolve_period(
                sess_preset, fin_year_start_month=fin_year_start_month
            )
        elif (
            sess_preset == "custom"
            and request.session.get("period_from")
            and request.session.get("period_to")
        ):
            from_ = request.session["period_from"]
            to_ = request.session["period_to"]
            active = "custom"
        else:
            from_, to_, active = default_from, default_to, "custom"

    request.session["period_preset"] = active
    request.session["period_from"] = from_
    request.session["period_to"] = to_
    return from_, to_, active


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


@router.get("/reports/close-year", response_class=HTMLResponse, response_model=None)
async def close_year_form(
    request: Request,
    through: str | None = None,
    retained_earnings_account_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Year-end close — preview the zeroing entry (ADMIN only)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    through_ = through or _last_fy_end()
    equity_accounts: list = []
    preview: dict = {}
    error: str | None = None
    degraded = False
    retained: str | None = None

    try:
        async with api_client(request) as client:
            if not through:
                # Default "through" date is the end of the LAST completed
                # financial year — derive it from the company's actual
                # fin_year_start_month, not a hardcoded 30 June (a non-AU /
                # calendar-year-FY company's last FY doesn't end 30 June).
                fin_year_start_month = await _fetch_fin_year_start_month(client)
                through_ = _last_fy_end(fin_year_start_month)

            acc_resp = await client.get(
                "/api/v1/accounts", params={"account_type": "EQUITY", "limit": 200}
            )
            if acc_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if acc_resp.is_success:
                equity_accounts = acc_resp.json().get("items", [])

            retained = retained_earnings_account_id or next(
                (
                    a["id"]
                    for a in equity_accounts
                    if "retained" in (a.get("name", "") or "").lower()
                ),
                (equity_accounts[0]["id"] if equity_accounts else None),
            )
            if retained:
                pv = await client.get(
                    "/api/v1/period-close/preview",
                    params={
                        "through_date": through_,
                        "retained_earnings_account_id": retained,
                    },
                )
                if pv.is_success:
                    preview = pv.json()
                else:
                    error = _("The report could not be loaded (HTTP %(code)s).") % {"code": pv.status_code}
            else:
                error = "No equity account found for retained earnings."
    except ModuleUnavailable:
        degraded = True

    flash = request.session.pop("flash", None)
    ctx = {
        "through": through_,
        "equity_accounts": equity_accounts,
        "retained_earnings_id": retained_earnings_account_id
        or (preview and retained)
        or "",
        "preview": preview,
        "error": error,
        "flash": flash,
        "degraded": degraded,
    }
    return _TEMPLATES.TemplateResponse(request, "reports/close_year.html", ctx)


@router.post("/reports/close-year", response_class=HTMLResponse, response_model=None)
async def close_year_submit(request: Request) -> RedirectResponse:
    """Post the year-end close journal + lock the period (ADMIN only)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form = await request.form()
    through_ = str(form.get("through", "")).strip()
    retained = str(form.get("retained_earnings_account_id", "")).strip()
    override = str(form.get("override_reason", "")).strip()
    if not through_ or not retained:
        request.session["flash"] = (
            "Through date and retained-earnings account are required."
        )
        return RedirectResponse(url="/reports/close-year", status_code=303)

    body: dict[str, str] = {
        "through_date": through_,
        "retained_earnings_account_id": retained,
    }
    if override:
        body["override_reason"] = override

    async with api_client(request) as client:
        resp = await client.post("/api/v1/period-close/close-year", json=body)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        data = resp.json()
        if data.get("closed") and data.get("journal_entry_id"):
            request.session["flash"] = "Year-end close posted; period locked."
            return RedirectResponse(
                url=f"/journal-entries/{data['journal_entry_id']}", status_code=303
            )
        request.session["flash"] = (
            "Nothing to close — every P&L account is already zero for that period."
        )
        return RedirectResponse(
            url=f"/reports/close-year?through={through_}", status_code=303
        )

    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(
        url=f"/reports/close-year?through={through_}", status_code=303
    )


def _subtract_one_year(d: date) -> date:
    """Subtract exactly one calendar year from d, with a safe leap-day fallback.

    2025-02-28 → 2024-02-28 (normal)
    2024-02-29 → 2023-02-28 (29 Feb only exists in a leap year; clamp to 28 Feb)
    """
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        # Only possible when d is 29 Feb in a leap year; prior year has no 29 Feb.
        return d.replace(year=d.year - 1, day=28)


def _build_comparative_lines(
    current_lines: list[dict],
    prior_lines: list[dict],
) -> list[dict]:
    """Merge two account-line lists into a single list suitable for comparative rendering.

    Each entry in the result has:
      account_id, account_name, code, current_amount, prior_amount

    Accounts present in only one period appear with 0.0 in the absent column.
    Order: current-period accounts first (in their original order), then any
    prior-only accounts appended at the end.
    """
    prior_by_id: dict[str, dict] = {
        line["account_id"]: line for line in prior_lines if line.get("account_id")
    }
    current_ids: set[str] = set()
    merged: list[dict] = []

    for line in current_lines:
        aid = line.get("account_id", "")
        current_ids.add(aid)
        prior = prior_by_id.get(aid, {})
        merged.append({
            "account_id": aid,
            "account_name": line.get("account_name") or line.get("name", "—"),
            "code": line.get("code", ""),
            "current_amount": float(line.get("amount", line.get("balance", 0)) or 0),
            "prior_amount": float(prior.get("amount", prior.get("balance", 0)) or 0),
        })

    # Accounts in prior but not in current — append with current_amount = 0.
    for aid, line in prior_by_id.items():
        if aid not in current_ids:
            merged.append({
                "account_id": aid,
                "account_name": line.get("account_name") or line.get("name", "—"),
                "code": line.get("code", ""),
                "current_amount": 0.0,
                "prior_amount": float(line.get("amount", line.get("balance", 0)) or 0),
            })

    return merged


def _extract_pl_lines(report: dict, section: str, key: str) -> list[dict]:
    """Extract a flat list of account lines from a P&L section dict."""
    return report.get(section, {}).get(key, [])


def _extract_bs_lines(report: dict, section: str, key: str) -> list[dict]:
    """Extract asset/liability/equity lines from a BS section dict."""
    return report.get(section, {}).get(key, [])


def _build_comparative_pl(current: dict, prior: dict) -> dict:
    """Build a comparative P&L structure for template rendering.

    Returns a dict with income/expenses sections, each containing merged
    comparative line lists and totals for both periods.
    """
    income_keys = ["INCOME", "OTHER_INCOME"]
    expense_keys = ["EXPENSE", "COST_OF_SALES", "OTHER_EXPENSE"]

    c_income: dict = current.get("income", {})
    p_income: dict = prior.get("income", {})
    c_expenses: dict = current.get("expenses", {})
    p_expenses: dict = prior.get("expenses", {})

    income_sections: dict[str, list] = {}
    for k in income_keys:
        income_sections[k] = _build_comparative_lines(
            c_income.get(k, []), p_income.get(k, [])
        )

    expense_sections: dict[str, list] = {}
    for k in expense_keys:
        expense_sections[k] = _build_comparative_lines(
            c_expenses.get(k, []), p_expenses.get(k, [])
        )

    return {
        "income": {**income_sections, "total_income_current": float(c_income.get("total_income", 0) or 0), "total_income_prior": float(p_income.get("total_income", 0) or 0)},
        "expenses": {**expense_sections, "total_expenses_current": float(c_expenses.get("total_expenses", 0) or 0), "total_expenses_prior": float(p_expenses.get("total_expenses", 0) or 0)},
        "net_profit_current": float(current.get("net_profit", 0) or 0),
        "net_profit_prior": float(prior.get("net_profit", 0) or 0),
    }


def _build_comparative_bs(current: dict, prior: dict) -> dict:
    """Build a comparative Balance Sheet structure for template rendering.

    NOTE: The prior BS is computed as_of the prior year-end; its CYE line
    reflects that year's own earnings, not the current year's. This is
    intentional — each period's CYE is computed by the engine independently.
    """
    c_assets = current.get("assets", {})
    p_assets = prior.get("assets", {})
    c_liabilities = current.get("liabilities", {})
    p_liabilities = prior.get("liabilities", {})
    c_equity = current.get("equity", {})
    p_equity = prior.get("equity", {})

    return {
        "assets": {
            "ASSET": _build_comparative_lines(c_assets.get("ASSET", []), p_assets.get("ASSET", [])),
            "total_assets_current": float(c_assets.get("total_assets", 0) or 0),
            "total_assets_prior": float(p_assets.get("total_assets", 0) or 0),
        },
        "liabilities": {
            "LIABILITY": _build_comparative_lines(c_liabilities.get("LIABILITY", []), p_liabilities.get("LIABILITY", [])),
            "total_liabilities_current": float(c_liabilities.get("total_liabilities", 0) or 0),
            "total_liabilities_prior": float(p_liabilities.get("total_liabilities", 0) or 0),
        },
        "equity": {
            "EQUITY": _build_comparative_lines(c_equity.get("EQUITY", []), p_equity.get("EQUITY", [])),
            "total_equity_current": float(c_equity.get("total_equity", 0) or 0),
            "total_equity_prior": float(p_equity.get("total_equity", 0) or 0),
        },
        "balanced": current.get("balanced", False),
        "difference": float(current.get("difference", 0) or 0),
    }


@router.get("/reports/statement-pack", response_class=HTMLResponse, response_model=None)
async def statement_pack(
    request: Request,
    as_of_date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    comparative: bool = True,
) -> HTMLResponse | RedirectResponse:
    """Financial statement pack — P&L + Balance Sheet + Trial Balance bundled
    into one printable document with a cover page and trustee declaration.

    Read-only: reuses the existing /api/v1/reports/* endpoints and the
    per-statement table fragments. Defaults to the current AU financial year
    (1 Jul → today). Use the Print button to save the whole pack as one PDF.

    When comparative=true (default) also fetches the prior FY P&L and the
    prior year-end Balance Sheet in the same asyncio.gather, and renders a
    second "Prior year" column next to each current-period amount.

    Prior-year CYE note: the prior BS is fetched as_of the prior year-end so
    the engine computes that year's own Current Year Earnings independently.
    This avoids double-counting the current year's CYE in the prior column.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()
    as_of = as_of_date or _today()

    pl_report: dict = {}
    bs_report: dict = {}
    tb_report: dict = {}
    pl_prior_raw: dict = {}
    bs_prior_raw: dict = {}
    company: dict = {}
    error: str | None = None
    degraded = False
    # Default fy_start below is overwritten once the company's actual
    # fin_year_start_month is known; kept as a same-shape fallback so the
    # variable is always bound even if the company fetch below fails.
    fy_start = date(today.year if today.month >= 7 else today.year - 1, 7, 1).isoformat()
    from_ = from_date or fy_start
    to_ = to_date or as_of

    try:
        async with api_client(request) as client:
            # Fetch the company FIRST (not in the big gather below) — its
            # fin_year_start_month drives the default `from_` (statement
            # pack defaults to "this financial year to date", which for a
            # non-AU / non-default company is NOT 1 July). Reused as the
            # `company` context var below instead of a second fetch.
            co_resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
            if co_resp.is_success:
                items = co_resp.json().get("items", [])
                company = items[0] if items else {}
            if not from_date:
                fin_year_start_month = company.get("fin_year_start_month") or 7
                fy_start_d, _fy_end_d = period.fy_bounds_containing(
                    today, fin_year_start_month=fin_year_start_month
                )
                from_ = fy_start_d.isoformat()
                to_ = to_date or as_of

            # Derive prior-period dates (safe leap-day guard via _subtract_one_year).
            from_date_obj = date.fromisoformat(from_)
            to_date_obj = date.fromisoformat(to_)
            as_of_obj = date.fromisoformat(as_of)
            prior_from = _subtract_one_year(from_date_obj).isoformat()
            prior_to = _subtract_one_year(to_date_obj).isoformat()
            prior_as_of = _subtract_one_year(as_of_obj).isoformat()

            if comparative:
                (
                    pl_resp, bs_resp, tb_resp,
                    pl_prior_resp, bs_prior_resp,
                ) = await asyncio.gather(
                    client.get(
                        "/api/v1/reports/profit_loss",
                        params={"from_date": from_, "to_date": to_},
                    ),
                    client.get(
                        "/api/v1/reports/balance_sheet", params={"as_of_date": as_of}
                    ),
                    client.get(
                        "/api/v1/reports/trial_balance", params={"as_of_date": as_of}
                    ),
                    # Prior-year fetches — in same gather to avoid serialisation.
                    client.get(
                        "/api/v1/reports/profit_loss",
                        params={"from_date": prior_from, "to_date": prior_to},
                    ),
                    client.get(
                        "/api/v1/reports/balance_sheet",
                        params={"as_of_date": prior_as_of},
                    ),
                )
                if pl_prior_resp.is_success:
                    pl_prior_raw = pl_prior_resp.json()
                if bs_prior_resp.is_success:
                    bs_prior_raw = bs_prior_resp.json()
            else:
                pl_resp, bs_resp, tb_resp = await asyncio.gather(
                    client.get(
                        "/api/v1/reports/profit_loss",
                        params={"from_date": from_, "to_date": to_},
                    ),
                    client.get(
                        "/api/v1/reports/balance_sheet", params={"as_of_date": as_of}
                    ),
                    client.get(
                        "/api/v1/reports/trial_balance", params={"as_of_date": as_of}
                    ),
                )

            if 401 in (pl_resp.status_code, bs_resp.status_code, tb_resp.status_code):
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if pl_resp.is_success:
                pl_report = pl_resp.json()
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": pl_resp.status_code}
            if bs_resp.is_success:
                bs_report = bs_resp.json()
            if tb_resp.is_success:
                tb_report = tb_resp.json()
    except ModuleUnavailable:
        degraded = True
    # prior_from/prior_to/prior_as_of are only bound inside the try block
    # above; if a ModuleUnavailable was raised before they were computed
    # (company fetch itself failing at the connection level), fall back to
    # the safe pre-loop defaults so the ctx dict build below never NameErrors.
    if degraded:
        from_date_obj = date.fromisoformat(from_)
        to_date_obj = date.fromisoformat(to_)
        as_of_obj = date.fromisoformat(as_of)
        prior_from = _subtract_one_year(from_date_obj).isoformat()
        prior_to = _subtract_one_year(to_date_obj).isoformat()
        prior_as_of = _subtract_one_year(as_of_obj).isoformat()

    # Build comparative data structures (empty when comparative=False).
    comp_pl: dict = {}
    comp_bs: dict = {}
    if comparative and pl_report and pl_prior_raw:
        comp_pl = _build_comparative_pl(pl_report, pl_prior_raw)
    if comparative and bs_report and bs_prior_raw:
        comp_bs = _build_comparative_bs(bs_report, bs_prior_raw)

    ctx = {
        "pl_report": pl_report,
        "bs_report": bs_report,
        "tb_report": tb_report,
        "company": company,
        "as_of_date": as_of,
        "from_date": from_,
        "to_date": to_,
        "prepared": _today(),
        "error": error,
        "degraded": degraded,
        # Comparative context — passed to pack-local markup only.
        # The shared fragment includes (_profit_loss_table, _balance_sheet_table,
        # _trial_balance_table) receive only their own report variable and are
        # NOT modified, preserving backward compatibility for all other routes.
        "comparative": comparative,
        "comp_pl": comp_pl,
        "comp_bs": comp_bs,
        "prior_from": prior_from,
        "prior_to": prior_to,
        "prior_as_of": prior_as_of,
    }
    return _TEMPLATES.TemplateResponse(request, "reports/statement_pack.html", ctx)


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
    degraded = False

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
        "degraded": degraded,
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
    degraded = False

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
        "degraded": degraded,
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
    preset: str | None = None,
    include_draft: bool = False,
    comparative: bool = False,
) -> HTMLResponse | RedirectResponse:
    """Profit & Loss report — income/expense sections and net profit.

    When comparative=true, also fetches the prior-year P&L (each date minus 1 year)
    and passes comp_pl into the template context for the shared fragment to render
    a second "Prior year" column.  Default is False — single-column behaviour
    unchanged for all existing callers.

    ``preset`` (This FY / Last FY / Calendar YTD / Trailing 12 months / This
    quarter) resolves via ``saebooks_web.period`` and takes precedence over
    from_date/to_date; the resolved period is persisted to the session (see
    ``_resolve_period_for_request``) so other period-aware pages pick it up.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    report: dict = {}
    error: str | None = None
    gst: dict = {}
    comp_pl: dict = {}
    degraded = False
    from_ = from_date or _month_start()
    to_ = to_date or _today()
    prior_from = _subtract_one_year(date.fromisoformat(from_)).isoformat()
    prior_to = _subtract_one_year(date.fromisoformat(to_)).isoformat()
    active_preset = "custom"

    try:
        async with api_client(request) as client:
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=_month_start(), default_to=_today(),
            )
            from_date_obj = date.fromisoformat(from_)
            to_date_obj = date.fromisoformat(to_)
            prior_from = _subtract_one_year(from_date_obj).isoformat()
            prior_to = _subtract_one_year(to_date_obj).isoformat()

            if comparative:
                pl_resp, pl_prior_resp, ytd_resp = await asyncio.gather(
                    client.get(
                        "/api/v1/reports/profit_loss",
                        params={"from_date": from_, "to_date": to_, "include_draft": str(include_draft).lower()},
                    ),
                    client.get(
                        "/api/v1/reports/profit_loss",
                        params={"from_date": prior_from, "to_date": prior_to, "include_draft": str(include_draft).lower()},
                    ),
                    client.get("/api/v1/reports/ytd_turnover"),
                )
            else:
                pl_resp, ytd_resp = await asyncio.gather(
                    client.get(
                        "/api/v1/reports/profit_loss",
                        params={"from_date": from_, "to_date": to_, "include_draft": str(include_draft).lower()},
                    ),
                    client.get("/api/v1/reports/ytd_turnover"),
                )
                pl_prior_resp = None

            if pl_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if pl_resp.is_success:
                report = pl_resp.json()
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": pl_resp.status_code}

            if comparative and pl_prior_resp is not None and pl_prior_resp.is_success:
                comp_pl = _build_comparative_pl(report, pl_prior_resp.json())

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
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "include_draft": include_draft,
        "error": error,
        "gst": gst,
        "comparative": comparative,
        "comp_pl": comp_pl,
        "prior_from": prior_from,
        "prior_to": prior_to,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
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
    comparative: bool = False,
) -> HTMLResponse | RedirectResponse:
    """Balance sheet report — assets, liabilities, equity sections.

    When comparative=true, also fetches the prior year-end Balance Sheet
    (as_of minus 1 year) and passes comp_bs into the template context for the
    shared fragment to render a second "Prior year" column.  Default is False.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None
    comp_bs: dict = {}
    degraded = False

    as_of_obj = date.fromisoformat(as_of)
    prior_as_of = _subtract_one_year(as_of_obj).isoformat()

    try:
        async with api_client(request) as client:
            if comparative:
                bs_resp, bs_prior_resp = await asyncio.gather(
                    client.get("/api/v1/reports/balance_sheet", params={"as_of_date": as_of}),
                    client.get("/api/v1/reports/balance_sheet", params={"as_of_date": prior_as_of}),
                )
            else:
                bs_resp = await client.get(
                    "/api/v1/reports/balance_sheet",
                    params={"as_of_date": as_of},
                )
                bs_prior_resp = None

            if bs_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if bs_resp.is_success:
                report = bs_resp.json()
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": bs_resp.status_code}

            if comparative and bs_prior_resp is not None and bs_prior_resp.is_success:
                comp_bs = _build_comparative_bs(report, bs_prior_resp.json())
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
        "comparative": comparative,
        "comp_bs": comp_bs,
        "prior_as_of": prior_as_of,
        "degraded": degraded,
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
    degraded = False

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "error": error,
        "degraded": degraded,
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
    preset: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Cashflow statement — operating / investing / financing waterfall."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    report: dict = {}
    error: str | None = None
    degraded = False
    from_ = from_date or _month_start()
    to_ = to_date or _today()
    active_preset = "custom"

    try:
        async with api_client(request) as client:
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=_month_start(), default_to=_today(),
            )
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "error": error,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
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
    degraded = False

    params: dict = {"as_of_date": as_of}
    # Omit method param when "all" or not supplied
    if method and method != "all":
        params["method"] = method

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "method": method or "all",
        "error": error,
        "degraded": degraded,
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
    degraded = False

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "error": error,
        "degraded": degraded,
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
    comparative: bool = False,
) -> HTMLResponse | RedirectResponse:
    """Trial balance report — debits/credits/balance per account with balanced indicator.

    When comparative=true, also fetches the prior year-end Trial Balance
    (as_of minus 1 year) and passes comp_tb into the template context for the
    shared fragment to render a second "Prior year" column.  Default is False.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    as_of = as_of_date or _today()
    report: dict = {}
    error: str | None = None
    comp_tb: list[dict] = []
    degraded = False

    as_of_obj = date.fromisoformat(as_of)
    prior_as_of = _subtract_one_year(as_of_obj).isoformat()

    try:
        async with api_client(request) as client:
            if comparative:
                tb_resp, tb_prior_resp = await asyncio.gather(
                    client.get(
                        "/api/v1/reports/trial_balance",
                        params={
                            "as_of_date": as_of,
                            "include_zero_balance": str(include_zero_balance).lower(),
                        },
                    ),
                    client.get(
                        "/api/v1/reports/trial_balance",
                        params={
                            "as_of_date": prior_as_of,
                            "include_zero_balance": str(include_zero_balance).lower(),
                        },
                    ),
                )
            else:
                tb_resp = await client.get(
                    "/api/v1/reports/trial_balance",
                    params={
                        "as_of_date": as_of,
                        "include_zero_balance": str(include_zero_balance).lower(),
                    },
                )
                tb_prior_resp = None

            if tb_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if tb_resp.is_success:
                report = tb_resp.json()
            else:
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": tb_resp.status_code}

            if comparative and tb_prior_resp is not None and tb_prior_resp.is_success:
                # Build comparative line list: current accounts enriched with prior balance.
                prior_accounts = tb_prior_resp.json().get("accounts", [])
                prior_by_id: dict[str, dict] = {a["account_id"]: a for a in prior_accounts if a.get("account_id")}
                current_ids: set[str] = set()
                merged: list[dict] = []
                for acct in report.get("accounts", []):
                    aid = acct.get("account_id", "")
                    current_ids.add(aid)
                    prior = prior_by_id.get(aid, {})
                    merged.append({
                        **acct,
                        "prior_balance": float(prior.get("balance", 0) or 0),
                        "prior_debit_total": float(prior.get("debit_total", 0) or 0),
                        "prior_credit_total": float(prior.get("credit_total", 0) or 0),
                    })
                # Append prior-only accounts
                for aid, acct in prior_by_id.items():
                    if aid not in current_ids:
                        merged.append({
                            "account_id": aid,
                            "code": acct.get("code", ""),
                            "name": acct.get("name", ""),
                            "account_type": acct.get("account_type", ""),
                            "debit_total": 0.0,
                            "credit_total": 0.0,
                            "balance": 0.0,
                            "prior_balance": float(acct.get("balance", 0) or 0),
                            "prior_debit_total": float(acct.get("debit_total", 0) or 0),
                            "prior_credit_total": float(acct.get("credit_total", 0) or 0),
                        })
                comp_tb = merged
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "as_of_date": as_of,
        "include_zero_balance": include_zero_balance,
        "error": error,
        "comparative": comparative,
        "comp_tb": comp_tb,
        "prior_as_of": prior_as_of,
        "degraded": degraded,
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
    degraded = False

    params: dict = {"year": yr}
    if month is not None:
        params["month"] = month

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "year": yr,
        "month": month,
        "current_year": _current_year(),
        "error": error,
        "degraded": degraded,
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
    preset: str | None = None,
    segment_type: str = "project",
) -> HTMLResponse | RedirectResponse:
    """P&L by segment report — net profit per project/department/cost-centre."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    report: dict = {}
    error: str | None = None
    degraded = False
    from_ = from_date or _month_start()
    to_ = to_date or _month_end()
    active_preset = "custom"

    try:
        async with api_client(request) as client:
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=_month_start(), default_to=_month_end(),
            )
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "segment_type": segment_type,
        "error": error,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
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
    preset: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Revenue by customer report — invoiced revenue (ex-GST) per contact."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    report: dict = {}
    error: str | None = None
    degraded = False
    from_ = from_date or _month_start()
    to_ = to_date or _month_end()
    active_preset = "custom"

    try:
        async with api_client(request) as client:
            from_, to_, active_preset = await _resolve_period_for_request(
                request, client, preset, from_date, to_date,
                default_from=_month_start(), default_to=_month_end(),
            )
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True

    ctx = {
        "report": report,
        "from_date": from_,
        "to_date": to_,
        "error": error,
        "degraded": degraded,
        "active_preset": active_preset,
        "preset_options": _period_preset_options(),
    }

    template = (
        "reports/_revenue_by_customer_table.html"
        if _is_htmx(request)
        else "reports/revenue_by_customer.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# BAS / PAYG review worksheet — GUI-rebuild (BAS/PAYG review screens)
# ---------------------------------------------------------------------------


def _detail_or_status(resp) -> str:
    """Best-effort error detail string from a non-2xx API response."""
    try:
        return str(resp.json().get("detail", "")) or f"HTTP {resp.status_code}"
    except Exception:
        return f"HTTP {resp.status_code}"


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
    degraded = False

    try:
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
                error = _("The report could not be loaded (HTTP %(code)s).") % {"code": resp.status_code}

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
    except ModuleUnavailable:
        degraded = True

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
        "degraded": degraded,
    }
    template = (
        "reports/_bas_payg_table.html"
        if _is_htmx(request)
        else "reports/bas_payg.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)
@router.get("/reports/statement-pack/pdf", response_model=None)
async def statement_pack_pdf(
    request: Request,
    as_of_date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    comparative: bool = True,
) -> Response | RedirectResponse:
    """Proxy the statement pack PDF from the API.

    Forwards query params to ``/api/v1/reports/statement_pack.pdf`` and
    streams the PDF bytes back with the correct content-type and
    content-disposition headers.  Pattern mirrors ``quote_pdf`` in
    ``saebooks_web.routes.quotes``.
    """
    from fastapi import HTTPException

    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, str] = {}
    if as_of_date:
        params["as_of_date"] = as_of_date
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    params["comparative"] = "true" if comparative else "false"

    async with api_client(request) as client:
        resp = await client.get("/api/v1/reports/statement_pack.pdf", params=params)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        raise HTTPException(404, detail="Statement pack not found")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=f"Upstream returned {resp.status_code}")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/pdf"),
        headers={
            "Content-Disposition": resp.headers.get(
                "content-disposition", 'inline; filename="statement-pack.pdf"'
            ),
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )
