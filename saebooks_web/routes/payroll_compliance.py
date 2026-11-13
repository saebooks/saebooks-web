"""Payroll compliance screens (batch 9).

Routes:

  GET  /payroll/stp                                   Single Touch Payroll: pay events, overdue
  POST /payroll/stp/{id}/check                        list what the ATO would reject
  POST /payroll/stp/{id}/lodge                        lodge (engine refuses while switched off)
  POST /payroll/stp/{id}/lodged-elsewhere             record a lodgement made elsewhere
  POST /payroll/pay-runs/{id}/stp/rebuild             rebuild a run's unlodged pay event
  GET  /payroll/super                                 super owed by fund, due dates, mark paid
  POST /payroll/super/{id}/mark-paid                  post the payment and mark paid
  GET  /payroll/super/{id}/file                       super file (SAFF) download
  POST /payroll/pay-runs/{id}/super/build             work out super for a finalised run
  GET  /payroll/year-end                              end of year: totals, blockers, finalise
  POST /payroll/year-end/{year}/finalise              build the finalisation event
  GET  /payroll/year-end/{year}/income-statements/{employee_id}   printable
  GET  /payroll/pay-runs/{id}/bank-file               bank file (ABA) download
  POST /payroll/pay-runs/{id}/payslips/email          email payslips
  POST /payroll/settings                              leave owed on the balance sheet on/off

Engine endpoints: /api/v1/payroll/* and /api/v1/super_lodgements/{id}/lines.csv.
Nothing on these screens lodges with the ATO unless the engine has lodging
switched on; the screens say so plainly when it is off.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.routes.wages import _error_text

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _authed(request: Request) -> bool:
    return bool(request.session.get("api_token"))


def _back(form, default: str) -> str:
    back = str(form.get("back") or "").strip()
    # Only ever bounce back inside the payroll screens (no open redirect).
    if back.startswith("/payroll/") and "//" not in back and "\\" not in back:
        return back
    return default


def _login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _current_fy(today: date) -> int:
    return today.year + 1 if today.month >= 7 else today.year


# --------------------------------------------------------------------------- #
# Single Touch Payroll
# --------------------------------------------------------------------------- #


@router.get("/payroll/stp", response_class=HTMLResponse, response_model=None)
async def stp_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        r = await client.get("/api/v1/payroll/stp")
    if r.status_code == 401:
        request.session.clear()
        return _login()
    data = r.json() if r.is_success else None
    return _TEMPLATES.TemplateResponse(request, "payroll/stp.html", {
        "data": data,
        "error": None if r.is_success else f"Could not load pay events: {_error_text(r)}",
        "flash": request.session.pop("flash", None),
        "flash_error": request.session.pop("flash_error", None),
        "stp_problems": request.session.pop("stp_problems", None),
        "today": date.today().isoformat(),
    })


@router.post("/payroll/stp/{sub_id}/check", response_model=None)
async def stp_check(request: Request, sub_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, "/payroll/stp")
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/stp/{sub_id}/check")
    if not r.is_success:
        request.session["flash_error"] = f"Could not check the pay event: {_error_text(r)}"
    elif r.json().get("ready"):
        request.session["flash"] = (
            "Checked: every detail the ATO needs is there."
            + ("" if r.json().get("lodging_enabled") else
               " " + (r.json().get("lodging_off_message") or ""))
        )
    else:
        request.session["stp_problems"] = r.json().get("problems") or []
    return RedirectResponse(url=back, status_code=303)


@router.post("/payroll/stp/{sub_id}/lodge", response_model=None)
async def stp_lodge(request: Request, sub_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, "/payroll/stp")
    if form.get("declaration") != "yes":
        request.session["flash_error"] = "Tick the declaration to lodge. Nothing was sent."
        return RedirectResponse(url=back, status_code=303)
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/stp/{sub_id}/lodge", json={
            "signatory": str(form.get("signatory") or "").strip() or "Authorised person",
            "declaration_accepted": True,
        })
    if r.is_success:
        body = r.json()
        request.session["flash"] = (
            f"Lodged with the ATO: {str(body.get('status')).lower()}"
            + (f", receipt {body['ato_receipt_number']}" if body.get("ato_receipt_number") else "")
            + "."
        )
    else:
        request.session["flash_error"] = f"Not lodged: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


@router.post("/payroll/stp/{sub_id}/lodged-elsewhere", response_model=None)
async def stp_lodged_elsewhere(request: Request, sub_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, "/payroll/stp")
    reference = str(form.get("reference") or "").strip()
    lodged_on = str(form.get("lodged_on") or "").strip() or date.today().isoformat()
    if not reference:
        request.session["flash_error"] = "Enter the receipt or reference you got when you lodged it."
        return RedirectResponse(url=back, status_code=303)
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/stp/{sub_id}/lodged-elsewhere",
                              json={"reference": reference, "lodged_on": lodged_on})
    if r.is_success:
        request.session["flash"] = f"Recorded as lodged elsewhere, reference {reference}."
    else:
        request.session["flash_error"] = f"Not recorded: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


@router.post("/payroll/pay-runs/{run_id}/stp/rebuild", response_model=None)
async def stp_rebuild(request: Request, run_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, f"/payroll/pay-runs/{run_id}")
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/pay-runs/{run_id}/stp-event")
    if r.is_success:
        request.session["flash"] = "Pay event rebuilt from the current employee details."
    else:
        request.session["flash_error"] = f"Not rebuilt: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


# --------------------------------------------------------------------------- #
# Super
# --------------------------------------------------------------------------- #


@router.get("/payroll/super", response_class=HTMLResponse, response_model=None)
async def super_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        r = await client.get("/api/v1/payroll/super")
        if r.status_code == 401:
            request.session.clear()
            return _login()
        bank_accounts: list[dict] = []
        br = await client.get("/api/v1/bank_accounts", params={"limit": 200, "offset": 0})
        if br.is_success:
            bank_accounts = br.json().get("items", [])
    return _TEMPLATES.TemplateResponse(request, "payroll/super.html", {
        "data": r.json() if r.is_success else None,
        "error": None if r.is_success else f"Could not load super: {_error_text(r)}",
        "flash": request.session.pop("flash", None),
        "flash_error": request.session.pop("flash_error", None),
        "bank_accounts": bank_accounts,
        "today": date.today().isoformat(),
    })


@router.post("/payroll/super/{run_id}/mark-paid", response_model=None)
async def super_mark_paid(request: Request, run_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, "/payroll/super")
    bank = str(form.get("bank_account_id") or "").strip()
    paid_on = str(form.get("paid_on") or "").strip()
    if not bank or not paid_on:
        request.session["flash_error"] = "Choose the bank account and the date the super was paid."
        return RedirectResponse(url=back, status_code=303)
    body = {"bank_account_id": bank, "paid_on": paid_on}
    ref = str(form.get("reference") or "").strip()
    if ref:
        body["reference"] = ref[:64]
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/super/{run_id}/mark-paid", json=body)
    if r.is_success:
        request.session["flash"] = (
            f"Super of ${r.json().get('amount')} recorded as paid on {paid_on}: the "
            "payment is posted against super owing and the bank account."
        )
    else:
        request.session["flash_error"] = f"Not recorded: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


@router.get("/payroll/super/{run_id}/file", response_model=None)
async def super_file(request: Request, run_id: UUID) -> Response:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/super_lodgements/{run_id}/lines.csv")
    if not r.is_success:
        request.session["flash_error"] = f"Super file not available: {_error_text(r)}"
        return RedirectResponse(url="/payroll/super", status_code=303)
    return Response(
        content=r.content, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="super-{run_id}.csv"'},
    )


@router.post("/payroll/pay-runs/{run_id}/super/build", response_model=None)
async def super_build(request: Request, run_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, f"/payroll/pay-runs/{run_id}")
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/pay-runs/{run_id}/super")
    if r.is_success:
        request.session["flash"] = "Super worked out for this pay run. See Super owed for the due date."
    else:
        request.session["flash_error"] = f"Super not worked out: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


# --------------------------------------------------------------------------- #
# End of year
# --------------------------------------------------------------------------- #


@router.get("/payroll/year-end", response_class=HTMLResponse, response_model=None)
async def year_end_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    try:
        year = int(request.query_params.get("year") or _current_fy(date.today()))
    except ValueError:
        year = _current_fy(date.today())
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/payroll/year-end/{year}")
    if r.status_code == 401:
        request.session.clear()
        return _login()
    this_fy = _current_fy(date.today())
    return _TEMPLATES.TemplateResponse(request, "payroll/year_end.html", {
        "data": r.json() if r.is_success else None,
        "error": None if r.is_success else f"Could not load the year: {_error_text(r)}",
        "year": year,
        "years": list(range(this_fy, this_fy - 4, -1)),
        "flash": request.session.pop("flash", None),
        "flash_error": request.session.pop("flash_error", None),
    })


@router.post("/payroll/year-end/{year}/finalise", response_model=None)
async def year_end_finalise(request: Request, year: int) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = f"/payroll/year-end?year={year}"
    if form.get("confirm") != "yes":
        request.session["flash_error"] = "Tick the confirmation box to finalise the year."
        return RedirectResponse(url=back, status_code=303)
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/year-end/{year}/finalise", json={})
    if r.is_success:
        request.session["flash"] = (
            "Finalisation event made for the year. Lodge it (or lodge it elsewhere and "
            "record it) on the Single Touch Payroll page by 14 July."
        )
    else:
        request.session["flash_error"] = f"Year not finalised: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)


@router.get("/payroll/year-end/{year}/income-statements/{employee_id}",
            response_class=HTMLResponse, response_model=None)
async def income_statement_page(
    request: Request, year: int, employee_id: UUID,
) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/payroll/year-end/{year}/income-statements/{employee_id}")
    if not r.is_success:
        request.session["flash_error"] = f"Income statement not available: {_error_text(r)}"
        return RedirectResponse(url=f"/payroll/year-end?year={year}", status_code=303)
    return _TEMPLATES.TemplateResponse(request, "payroll/income_statement.html",
                                       {"st": r.json(), "year": year})


# --------------------------------------------------------------------------- #
# Pay run extras: bank file and payslip email
# --------------------------------------------------------------------------- #


@router.get("/payroll/pay-runs/{run_id}/bank-file", response_model=None)
async def bank_file(request: Request, run_id: UUID) -> Response:
    if not _authed(request):
        return _login()
    params = {}
    bank = str(request.query_params.get("bank_account_id") or "").strip()
    if bank:
        params["bank_account_id"] = bank
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/payroll/pay-runs/{run_id}/bank-file", params=params)
    if not r.is_success:
        request.session["flash_error"] = f"Bank file not made: {_error_text(r)}"
        return RedirectResponse(url=f"/payroll/pay-runs/{run_id}", status_code=303)
    return Response(
        content=r.content, media_type="text/plain",
        headers={"Content-Disposition": r.headers.get(
            "content-disposition", f'attachment; filename="wages-{run_id}.aba"')},
    )


@router.post("/payroll/pay-runs/{run_id}/payslips/email", response_model=None)
async def email_payslips(request: Request, run_id: UUID) -> RedirectResponse:
    if not _authed(request):
        return _login()
    back = f"/payroll/pay-runs/{run_id}"
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/payroll/pay-runs/{run_id}/payslips/email", json={})
    if not r.is_success:
        request.session["flash_error"] = f"Payslips not emailed: {_error_text(r)}"
        return RedirectResponse(url=back, status_code=303)
    body = r.json()
    request.session["email_results"] = body.get("results") or []
    request.session["flash"] = (
        f"Payslips emailed: {body.get('sent', 0)}. Not emailed: {body.get('not_sent', 0)} "
        "(see the list below)."
    )
    return RedirectResponse(url=back, status_code=303)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@router.post("/payroll/settings", response_model=None)
async def payroll_settings(request: Request) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    back = _back(form, "/payroll/pay-runs")
    on = form.get("leave_provision") == "yes"
    async with api_client(request) as client:
        r = await client.put("/api/v1/payroll/settings", json={"leave_provision": on})
    if r.is_success:
        request.session["flash"] = (
            "Leave owed will be shown on the balance sheet from the next pay run."
            if on else "Leave owed will no longer be booked to the balance sheet."
        )
    else:
        request.session["flash_error"] = f"Setting not saved: {_error_text(r)}"
    return RedirectResponse(url=back, status_code=303)
