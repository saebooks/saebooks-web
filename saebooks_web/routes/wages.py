"""Wages pay runs -- weekly payroll with tax computed by the engine.

The old ``/pay-run`` screen now redirects here (``routes/pay_run.py``).

Routes:

  GET  /payroll/pay-runs                              list + new pay run form
  POST /payroll/pay-runs/new                          create a draft pay run
  GET  /payroll/pay-runs/{id}                         review: lines, checks, totals
  POST /payroll/pay-runs/{id}/lines                   add / update one employee's hours
  POST /payroll/pay-runs/{id}/lines/{line_id}/delete  remove a line (draft only)
  POST /payroll/pay-runs/{id}/finalise                post the wages journal
  GET  /payroll/pay-runs/{id}/payslips/{employee_id}  printable payslip

Single Touch Payroll, super, the bank file and payslip email for a pay run
are in ``routes/payroll_compliance.py``.

Engine endpoints used (all /api/v1/pay-runs/*):
  GET/POST  /pay-runs                    list / create draft
  GET       /pay-runs/{id}/wages         review view (computed lines + checks)
  POST      /pay-runs/{id}/wage-lines    compute + store a line from hours
  DELETE    /pay-runs/{id}/lines/{lid}   remove a line (draft only)
  POST      /pay-runs/{id}/finalize-wages  re-verify + post the journal
  GET       /pay-runs/{id}/payslips/{employee_id}

PAYG, super and net are never entered here: the engine computes them from
the hours and the employee record, and posts the journal on finalise.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_BASE = "/payroll/pay-runs"


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _error_text(resp) -> str:
    try:
        body = resp.json()
    except Exception:  # not JSON
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message")
        if isinstance(detail, list):
            return "; ".join(str(d.get("msg", d)) for d in detail)
        if detail:
            return str(detail)
    return f"HTTP {resp.status_code}"


def month_end_for(start: date) -> date:
    """Last day of a one-month pay period beginning ``start`` (mirrors the
    engine's check: 1 Nov -> 30 Nov, 15 Jan -> 14 Feb, 31 Jan -> 28 Feb)."""
    import calendar

    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    last = calendar.monthrange(y, m)[1]
    if start.day > last:
        return date(y, m, last)
    return date(y, m, start.day) - timedelta(days=1)


#: STP Phase 2 allowance codes, in plain words.
ALLOWANCE_KINDS = [
    ("CD", "Cents per kilometre (car)"),
    ("AD", "Award transport"),
    ("LD", "Laundry"),
    ("MD", "Overtime meal"),
    ("RD", "Travel and accommodation"),
    ("TD", "Tools"),
    ("KN", "Tasks"),
    ("QN", "Qualifications and certificates"),
    ("OD", "Other"),
]

#: STP Phase 2 deduction codes, in plain words.
DEDUCTION_KINDS = [
    ("F", "Union or professional association fees"),
    ("W", "Workplace giving"),
    ("G", "Child support garnishee"),
    ("D", "Child support deduction"),
    ("OTHER", "Other deduction"),
]


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _dec(value: str | None) -> Decimal | None:
    if value is None or not str(value).strip():
        return Decimal("0")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return d if d >= 0 else None


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


@router.get(_BASE, response_class=HTMLResponse, response_model=None)
async def wage_runs_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    runs: list[dict] = []
    error: str | None = None
    async with api_client(request) as client:
        r = await client.get("/api/v1/pay-runs", params={"limit": 100, "offset": 0})
        if r.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if r.is_success:
            runs = r.json().get("items", [])
        else:
            error = f"Could not load pay runs: {_error_text(r)}"
        payroll_settings: dict = {}
        ps = await client.get("/api/v1/payroll/settings")
        if ps.is_success:
            payroll_settings = ps.json()
        stp_by_run: dict[str, dict] = {}
        sr = await client.get("/api/v1/payroll/stp")
        if sr.is_success:
            for item in sr.json().get("items", []):
                if item.get("pay_run_id"):
                    stp_by_run[str(item["pay_run_id"])] = item
    for run in runs:
        run["stp"] = stp_by_run.get(str(run.get("id")))
        lines = run.get("lines") or []
        run["employee_count"] = len(lines)
        for key, src in (("total_gross", "gross"), ("total_payg", "tax"),
                         ("total_super", "super_amount"), ("total_net", "net")):
            run[key] = str(sum((Decimal(str(ln.get(src) or "0")) for ln in lines), Decimal("0")))
    start = _next_monday(date.today())
    return _TEMPLATES.TemplateResponse(
        request,
        "payroll/pay_runs_list.html",
        {
            "runs": runs,
            "payroll_settings": payroll_settings,
            "flash_error": request.session.pop("flash_error", None),
            "error": error,
            "flash": request.session.pop("flash", None),
            "default_start": start.isoformat(),
            "default_end": (start + timedelta(days=6)).isoformat(),
            "default_pay_date": (start + timedelta(days=7)).isoformat(),
        },
    )


@router.post(f"{_BASE}/new", response_model=None)
async def wage_run_create(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    try:
        start = date.fromisoformat(str(form.get("period_start", "")))
        frequency = str(form.get("frequency") or "WEEKLY").upper()
        days = 14 if frequency == "FORTNIGHTLY" else 7
        end_raw = str(form.get("period_end") or "").strip()
        if frequency == "MONTHLY":
            default_end = month_end_for(start)
        else:
            default_end = start + timedelta(days=days - 1)
        end = date.fromisoformat(end_raw) if end_raw else default_end
        pay_date = date.fromisoformat(str(form.get("payment_date", "")))
    except ValueError:
        request.session["flash"] = "Enter a period start date and a pay date."
        return RedirectResponse(url=_BASE, status_code=303)
    if frequency == "MONTHLY":
        if end != month_end_for(start):
            request.session["flash"] = (
                f"A monthly pay run covers one month: {start.isoformat()} to "
                f"{month_end_for(start).isoformat()}."
            )
            return RedirectResponse(url=_BASE, status_code=303)
    elif (end - start).days + 1 != days:
        request.session["flash"] = (
            f"A {frequency.lower()} pay run covers {days} days; "
            f"{start.isoformat()} to {end.isoformat()} is {(end - start).days + 1}."
        )
        return RedirectResponse(url=_BASE, status_code=303)
    async with api_client(request) as client:
        r = await client.post("/api/v1/pay-runs", json={
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "payment_date": pay_date.isoformat(),
            "description": f"Wages {frequency.lower()} {start.isoformat()} to {end.isoformat()}",
        })
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not r.is_success:
        request.session["flash"] = f"Could not create the pay run: {_error_text(r)}"
        return RedirectResponse(url=_BASE, status_code=303)
    return RedirectResponse(url=f"{_BASE}/{r.json()['id']}", status_code=303)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@router.get(f"{_BASE}/{{run_id}}", response_class=HTMLResponse, response_model=None)
async def wage_run_detail(request: Request, run_id: UUID) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/pay-runs/{run_id}/wages")
        if r.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if r.status_code == 404:
            request.session["flash"] = "Pay run not found."
            return RedirectResponse(url=_BASE, status_code=303)
        employees: list[dict] = []
        er = await client.get("/api/v1/employees", params={"limit": 500, "offset": 0,
                                                          "only_active": "true"})
        if er.is_success:
            employees = er.json().get("items", [])
        # EmployeeOut carries contact_id, not the name: map it from contacts.
        if employees and not all(e.get("contact_name") for e in employees):
            cr = await client.get("/api/v1/contacts", params={"limit": 500, "offset": 0})
            if cr.is_success:
                names = {str(c["id"]): c.get("name") for c in cr.json().get("items", [])}
                for e in employees:
                    if not e.get("contact_name"):
                        e["contact_name"] = names.get(str(e.get("contact_id")))
        bank_accounts: list[dict] = []
        if r.is_success and str(r.json()["pay_run"].get("status") or "").upper() == "FINALIZED":
            br = await client.get("/api/v1/bank_accounts", params={"limit": 100, "offset": 0})
            if br.is_success:
                data = br.json()
                bank_accounts = data.get("items", data) if isinstance(data, dict) else data
    view = r.json() if r.is_success else None
    error = None if r.is_success else f"Could not load the pay run: {_error_text(r)}"
    on_run = {ln["employee_id"] for ln in (view or {}).get("lines", [])}
    return _TEMPLATES.TemplateResponse(
        request,
        "payroll/pay_run_detail.html",
        {
            "view": view,
            "run": (view or {}).get("pay_run"),
            "error": error,
            "flash": request.session.pop("flash", None),
            "flash_error": request.session.pop("flash_error", None),
            "employees": employees,
            "on_run": on_run,
            "allowance_kinds": ALLOWANCE_KINDS,
            "deduction_kinds": DEDUCTION_KINDS,
            "stp_problems": request.session.pop("stp_problems", None),
            "email_results": request.session.pop("email_results", None),
            "bank_accounts": bank_accounts,
            "today": date.today().isoformat(),
        },
    )


@router.post(f"{_BASE}/{{run_id}}/lines", response_model=None)
async def wage_line_upsert(request: Request, run_id: UUID) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    back = f"{_BASE}/{run_id}"
    employee_id = str(form.get("employee_id") or "").strip()
    ordinary = _dec(form.get("ordinary_hours"))
    overtime = _dec(form.get("overtime_hours"))
    multiplier = _dec(form.get("overtime_multiplier") or "1.5")
    if not employee_id or ordinary is None or overtime is None or multiplier is None:
        request.session["flash_error"] = "Choose an employee and enter hours as numbers."
        return RedirectResponse(url=back, status_code=303)
    items, problem = pay_items_from_form(form)
    if problem:
        request.session["flash_error"] = problem
        return RedirectResponse(url=back, status_code=303)
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/pay-runs/{run_id}/wage-lines", json={
            "employee_id": employee_id,
            "ordinary_hours": str(ordinary),
            "overtime_hours": str(overtime),
            "overtime_multiplier": str(multiplier),
            **items,
        })
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if r.is_success:
        request.session["flash"] = "Hours saved and pay calculated."
    else:
        request.session["flash_error"] = f"Not saved: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


def pay_items_from_form(form) -> tuple[dict, str | None]:
    """Read the "More pay items" fields. Blank rows are ignored; a row with
    an amount that is not a number is refused (never silently dropped)."""
    def amt(name: str) -> Decimal | None | str:
        raw = str(form.get(name) or "").strip().replace(",", "").replace("$", "")
        if not raw:
            return None
        try:
            d = Decimal(raw)
        except InvalidOperation:
            return "bad"
        return d if d > 0 else "bad"

    out: dict = {"full_period_salary": form.get("full_period_salary") == "yes",
                 "allowances": [], "deductions": [], "leave_taken": [],
                 "additional_payments": []}
    for i in range(1, 4):
        a = amt(f"allowance_amount_{i}")
        if a is None:
            continue
        if a == "bad":
            return {}, f"Allowance {i}: enter the amount as a number more than $0."
        out["allowances"].append({
            "kind": str(form.get(f"allowance_kind_{i}") or "OD"),
            "description": str(form.get(f"allowance_description_{i}") or "")[:120],
            "amount": str(a),
            "taxable": form.get(f"allowance_taxable_{i}") == "yes",
            "counts_for_super": form.get(f"allowance_super_{i}") == "yes",
        })
    for i in range(1, 3):
        a = amt(f"deduction_amount_{i}")
        if a is None:
            continue
        if a == "bad":
            return {}, f"Deduction {i}: enter the amount as a number more than $0."
        out["deductions"].append({
            "kind": str(form.get(f"deduction_kind_{i}") or "OTHER"),
            "description": str(form.get(f"deduction_description_{i}") or "")[:120],
            "amount": str(a),
        })
    for i in range(1, 3):
        h = amt(f"leave_hours_{i}")
        if h is None:
            continue
        if h == "bad":
            return {}, f"Leave {i}: enter the hours as a number more than 0."
        loading = _dec(form.get(f"leave_loading_{i}") or "0")
        if loading is None:
            return {}, f"Leave {i}: enter the leave loading as a percentage, for example 17.5."
        out["leave_taken"].append({
            "leave_type": str(form.get(f"leave_type_{i}") or "ANNUAL"),
            "hours": str(h),
            "loading_percent": str(loading),
        })
    for i in range(1, 3):
        a = amt(f"extra_amount_{i}")
        if a is None:
            continue
        if a == "bad":
            return {}, f"Bonus or back pay {i}: enter the amount as a number more than $0."
        periods_raw = str(form.get(f"extra_periods_{i}") or "").strip()
        try:
            periods = int(periods_raw) if periods_raw else None
        except ValueError:
            return {}, f"Bonus or back pay {i}: the number of pay periods must be a whole number."
        out["additional_payments"].append({
            "kind": str(form.get(f"extra_kind_{i}") or "BONUS"),
            "description": str(form.get(f"extra_description_{i}") or "")[:120],
            "amount": str(a),
            "periods": periods,
            "counts_for_super": form.get(f"extra_super_{i}") == "yes",
            "prior_year": form.get(f"extra_prior_year_{i}") == "yes",
        })
    return out, None


@router.post(f"{_BASE}/{{run_id}}/lines/{{line_id}}/delete", response_model=None)
async def wage_line_delete(request: Request, run_id: UUID, line_id: UUID) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        r = await client.delete(f"/api/v1/pay-runs/{run_id}/lines/{line_id}")
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if r.is_success:
        request.session["flash"] = "Employee removed from this pay run."
    else:
        request.session["flash_error"] = f"Not removed: {_error_text(r)}"
    return RedirectResponse(url=f"{_BASE}/{run_id}", status_code=303)


@router.post(f"{_BASE}/{{run_id}}/finalise", response_model=None)
async def wage_run_finalise(request: Request, run_id: UUID) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    back = f"{_BASE}/{run_id}"
    if form.get("confirm") != "yes":
        request.session["flash_error"] = (
            "Tick the confirmation box to finalise. Nothing was posted."
        )
        return RedirectResponse(url=back, status_code=303)
    try:
        body = {
            "expected_line_count": int(str(form.get("expected_line_count"))),
            "expected_total_net": str(Decimal(str(form.get("expected_total_net")))),
            "expected_total_payg": str(Decimal(str(form.get("expected_total_payg")))),
        }
    except (InvalidOperation, ValueError):
        request.session["flash_error"] = "The review figures were missing. Reload and try again."
        return RedirectResponse(url=back, status_code=303)
    async with api_client(request) as client:
        r = await client.post(
            f"/api/v1/pay-runs/{run_id}/finalize-wages",
            json=body,
            headers={"If-Match": str(form.get("version") or "")},
        )
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if r.is_success:
        request.session["flash"] = (
            "Pay run finalised: wages journal posted. Now pay the net amounts (bank "
            "file below), report the pay event to the ATO, pay the super by its due "
            "date and give each employee their payslip within one working day."
        )
    else:
        request.session["flash_error"] = f"Not finalised: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


# ---------------------------------------------------------------------------
# Payslip (printable)
# ---------------------------------------------------------------------------


@router.get(f"{_BASE}/{{run_id}}/payslips/{{employee_id}}", response_class=HTMLResponse,
            response_model=None)
async def wage_payslip(request: Request, run_id: UUID, employee_id: UUID) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/pay-runs/{run_id}/payslips/{employee_id}")
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not r.is_success:
        request.session["flash_error"] = f"Payslip not available: {_error_text(r)}"
        return RedirectResponse(url=f"{_BASE}/{run_id}", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "payroll/payslip.html",
        {"slip": r.json()["payslip"], "run_id": str(run_id)},
    )
