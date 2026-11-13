"""Payroll setup screens (gaps P2-20, P2-21; 2026-11-13 sweep).

* ``GET  /payroll/accounts`` — the accounts the wages journal posts to,
  whether each is ready, missing or the wrong kind, and what payroll owes
  right now (tax withheld, super, net wages), from
  ``GET /api/v1/payroll-setup/accounts``.
* ``POST /payroll/accounts`` — create every missing payroll account in one
  step (``POST /api/v1/payroll-setup/accounts``). Owners and admins only.
  Existing accounts are never changed; a wrong-kind account is listed for a
  person to fix.
* ``GET  /employees/{id}/readiness`` — HTMX card on the employee page:
  what is missing before this person can be paid (tax file number check,
  bank, super fund, pay rate, visa), from
  ``GET /api/v1/payroll-setup/employees/{id}/readiness``.
"""
from __future__ import annotations

import uuid
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


def _is_admin(request: Request) -> bool:
    role = request.session.get("user_role", "")
    return bool(request.session.get("is_sae_staff")) or role in ("owner", "admin")


@router.get("/payroll/accounts", response_class=HTMLResponse, response_model=None)
async def payroll_accounts(request: Request) -> HTMLResponse | RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    data: dict = {}
    error = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/payroll-setup/accounts")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            data = resp.json()
        else:
            error = _("Payroll accounts could not be checked (HTTP %(code)s).") % {"code": resp.status_code}
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/payroll_accounts.html",
        {
            "data": data,
            "error": error,
            "degraded": degraded,
            "can_edit": _is_admin(request),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/payroll/accounts", response_model=None)
async def create_payroll_accounts(request: Request) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    back = RedirectResponse(url="/payroll/accounts", status_code=303)
    if not _is_admin(request):
        request.session["flash"] = _("Only an owner or admin can create accounts.")
        return back
    async with api_client(request) as client:
        resp = await client.post("/api/v1/payroll-setup/accounts")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        body = resp.json()
        created = body.get("created", [])
        left = body.get("still_to_fix", [])
        if created:
            msg = _("Created %(n)s payroll accounts: %(codes)s.") % {
                "n": len(created), "codes": ", ".join(a["code"] for a in created)}
        else:
            msg = _("No accounts were missing.")
        if left:
            msg += " " + _("%(n)s still need fixing by hand (see below).") % {"n": len(left)}
        request.session["flash"] = msg
    else:
        request.session["flash"] = _("Could not create the accounts (HTTP %(code)s).") % {"code": resp.status_code}
    return back


@router.get("/employees/{employee_id}/readiness", response_class=HTMLResponse, response_model=None)
async def employee_readiness(request: Request, employee_id: uuid.UUID) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    data: dict | None = None
    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/payroll-setup/employees/{employee_id}/readiness")
        if resp.is_success:
            data = resp.json()
    except ModuleUnavailable:
        data = None
    return _TEMPLATES.TemplateResponse(
        request, "b15/employee_readiness.html", {"data": data, "employee_id": str(employee_id)}
    )
