"""Employee web routes — list / create / detail / edit / terminate / TFN reveal.

Routes:

  GET  /employees                 — list (filters: search, only_active, super_fund_id)
  GET  /employees/new             — create form
  POST /employees/new             — submit create
  GET  /employees/{id}            — detail (masked TFN; bank shows configured/not set)
  GET  /employees/{id}/edit       — edit form
  POST /employees/{id}/edit       — submit edit with If-Match
  POST /employees/{id}/terminate  — set end_date + termination_reason
  POST /employees/{id}/reveal-tfn — call /api/v1/employees/{id}/tfn; render inline
  POST /employees/{id}/archive    — soft-delete
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


async def _fetch_contacts(client) -> list[dict]:
    resp = await client.get("/api/v1/contacts", params={"limit": 500, "offset": 0})
    if resp.is_success:
        return resp.json().get("items", [])
    return []


async def _fetch_super_funds(client) -> list[dict]:
    resp = await client.get("/api/v1/super-funds", params={"limit": 200, "offset": 0})
    if resp.is_success:
        return resp.json().get("items", [])
    return []


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/employees", response_class=HTMLResponse, response_model=None)
async def employees_list(
    request: Request,
    search: str | None = None,
    only_active: bool = False,
    super_fund_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if search:
        params["search"] = search
    if only_active:
        params["only_active"] = "true"
    if super_fund_id:
        params["super_fund_id"] = super_fund_id

    employees: list[dict] = []
    total = 0
    error: str | None = None
    super_funds: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/employees", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            employees = payload.get("items", [])
            total = payload.get("total", len(employees))
        else:
            error = f"API error: HTTP {resp.status_code}"
        super_funds = await _fetch_super_funds(client)

    fund_by_id = {str(f["id"]): f for f in super_funds}

    return _TEMPLATES.TemplateResponse(
        request,
        "employees/list.html",
        {
            "employees": employees,
            "total": total,
            "error": error,
            "limit": limit,
            "offset": offset,
            "prev_offset": max(offset - limit, 0) if offset > 0 else None,
            "next_offset": offset + limit if (offset + limit) < total else None,
            "super_funds": super_funds,
            "fund_by_id": fund_by_id,
            "filter_search": search or "",
            "filter_only_active": only_active,
            "filter_super_fund_id": super_fund_id or "",
        },
    )


# ---------------------------------------------------------------------------
# New
# ---------------------------------------------------------------------------


@router.get("/employees/new", response_class=HTMLResponse, response_model=None)
async def employee_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    contacts: list[dict] = []
    super_funds: list[dict] = []
    async with api_client(request) as client:
        contacts = await _fetch_contacts(client)
        super_funds = await _fetch_super_funds(client)

    default_fund = next((f for f in super_funds if f.get("is_default")), None)

    return _TEMPLATES.TemplateResponse(
        request,
        "employees/new.html",
        {
            "form": {
                "contact_id": "",
                "employee_number": "",
                "start_date": date.today().isoformat(),
                "dob": "",
                "tfn": "",
                "tfn_status": "NOT_PROVIDED",
                "employment_basis": "F",
                "claims_tax_free_threshold": False,
                "is_australian_resident": True,
                "study_training_support_loan": False,
                "working_holiday_maker": False,
                "whm_country_code": "",
                "income_stream_type": "SAW",
                "payg_branch_code": "",
                "tax_treatment_code": "",
                "address_line1": "",
                "address_line2": "",
                "suburb": "",
                "state": "",
                "postcode": "",
                "country_code": "AU",
                "pay_frequency": "WEEKLY",
                "pay_basis": "HOURLY",
                "base_rate": "",
                "weekly_hours": "38.00",
                "payslip_email": "",
                "payslip_delivery": "EMAIL",
                "super_fund_id": str(default_fund["id"]) if default_fund else "",
                "super_member_number": "",
                "bsb": "",
                "account_number": "",
                "account_name": "",
            },
            "errors": {},
            "contacts": contacts,
            "super_funds": super_funds,
        },
    )


@router.post("/employees/new", response_class=HTMLResponse, response_model=None)
async def employee_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    payload: dict[str, object] = {}

    # Required fields
    if contact_id := form.get("contact_id", "").strip():
        payload["contact_id"] = contact_id
    if start_date := form.get("start_date", "").strip():
        payload["start_date"] = start_date
    if employment_basis := form.get("employment_basis", "").strip():
        payload["employment_basis"] = employment_basis
    base_rate = _parse_decimal(form.get("base_rate"))
    if base_rate is not None:
        payload["base_rate"] = str(base_rate)

    # Optional identity
    for field in ("employee_number", "dob", "payee_id_bms"):
        if val := form.get(field, "").strip():
            payload[field] = val

    # Sensitive write-only
    for field in ("tfn", "bsb", "account_number", "account_name"):
        if val := form.get(field, "").strip():
            payload[field] = val

    # Tax/STP selects + strings
    for field in (
        "tfn_status",
        "income_stream_type",
        "payg_branch_code",
        "tax_treatment_code",
        "whm_country_code",
    ):
        if val := form.get(field, "").strip():
            payload[field] = val

    # Booleans (checkbox = "on" when ticked)
    for field in (
        "claims_tax_free_threshold",
        "is_australian_resident",
        "study_training_support_loan",
        "working_holiday_maker",
    ):
        payload[field] = form.get(field) in ("on", "true", "1")

    # Address
    for field in ("address_line1", "address_line2", "suburb", "state", "postcode"):
        if val := form.get(field, "").strip():
            payload[field] = val
    payload["country_code"] = form.get("country_code", "AU").strip() or "AU"

    # Pay shape
    for field in ("pay_frequency", "pay_basis", "payslip_delivery"):
        if val := form.get(field, "").strip():
            payload[field] = val
    weekly_hours = _parse_decimal(form.get("weekly_hours"))
    if weekly_hours is not None:
        payload["weekly_hours"] = str(weekly_hours)
    if payslip_email := form.get("payslip_email", "").strip():
        payload["payslip_email"] = payslip_email

    # Super + bank
    if super_fund_id := form.get("super_fund_id", "").strip():
        payload["super_fund_id"] = super_fund_id
    if super_member_number := form.get("super_member_number", "").strip():
        payload["super_member_number"] = super_member_number

    async with api_client(request) as client:
        resp = await client.post("/api/v1/employees", json=payload)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code in (200, 201):
            emp_id = resp.json()["id"]
            return RedirectResponse(url=f"/employees/{emp_id}", status_code=303)

        errors: dict[str, str] = {}
        try:
            err_body = resp.json()
            errors["_global"] = err_body.get("detail") or f"HTTP {resp.status_code}"
        except Exception:
            errors["_global"] = f"HTTP {resp.status_code}"

        contacts = await _fetch_contacts(client)
        super_funds = await _fetch_super_funds(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "employees/new.html",
        {
            "form": form,
            "errors": errors,
            "contacts": contacts,
            "super_funds": super_funds,
        },
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/employees/{employee_id}", response_class=HTMLResponse, response_model=None)
async def employee_detail(
    employee_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    employee: dict | None = None
    contact: dict | None = None
    super_fund: dict | None = None
    error: str | None = None
    tfn_plaintext: str | None = None

    # Check if TFN was just revealed (stored briefly in session)
    reveal_key = f"tfn_reveal_{employee_id}"
    if request.session.get(reveal_key):
        tfn_plaintext = request.session.pop(reveal_key)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/employees/{employee_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            employee = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"
            return _TEMPLATES.TemplateResponse(
                request,
                "employees/detail.html",
                {"employee": None, "error": error},
                status_code=404,
            )

        # Fetch linked contact
        if employee and employee.get("contact_id"):
            c_resp = await client.get(f"/api/v1/contacts/{employee['contact_id']}")
            if c_resp.is_success:
                contact = c_resp.json()

        # Fetch linked super fund
        if employee and employee.get("super_fund_id"):
            sf_resp = await client.get(f"/api/v1/super-funds/{employee['super_fund_id']}")
            if sf_resp.is_success:
                super_fund = sf_resp.json()

    return _TEMPLATES.TemplateResponse(
        request,
        "employees/detail.html",
        {
            "employee": employee,
            "contact": contact,
            "super_fund": super_fund,
            "error": error,
            "tfn_plaintext": tfn_plaintext,
        },
    )


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@router.get(
    "/employees/{employee_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def employee_edit_form(
    employee_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    employee: dict | None = None
    contacts: list[dict] = []
    super_funds: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/employees/{employee_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            employee = resp.json()
        contacts = await _fetch_contacts(client)
        super_funds = await _fetch_super_funds(client)

    if employee is None:
        return RedirectResponse(url="/employees", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "employees/edit.html",
        {
            "employee": employee,
            "form": employee,
            "errors": {},
            "contacts": contacts,
            "super_funds": super_funds,
        },
    )


@router.post(
    "/employees/{employee_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def employee_edit_submit(
    employee_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    payload: dict[str, object] = {}

    for field in (
        "contact_id",
        "employee_number",
        "start_date",
        "dob",
        "employment_basis",
        "tax_treatment_code",
        "income_stream_type",
        "payg_branch_code",
        "whm_country_code",
        "address_line1",
        "address_line2",
        "suburb",
        "state",
        "postcode",
        "country_code",
        "pay_frequency",
        "pay_basis",
        "payslip_email",
        "payslip_delivery",
        "super_fund_id",
        "super_member_number",
        "tfn_status",
    ):
        if field in form_data:
            val = form.get(field, "").strip()
            payload[field] = val or None

    base_rate = _parse_decimal(form.get("base_rate"))
    if base_rate is not None:
        payload["base_rate"] = str(base_rate)
    weekly_hours = _parse_decimal(form.get("weekly_hours"))
    if weekly_hours is not None:
        payload["weekly_hours"] = str(weekly_hours)

    for field in (
        "claims_tax_free_threshold",
        "is_australian_resident",
        "study_training_support_loan",
        "working_holiday_maker",
    ):
        if field in form_data:
            payload[field] = form.get(field) in ("on", "true", "1")

    # Sensitive write paths — only send if non-empty
    for field in ("tfn", "bsb", "account_number", "account_name"):
        if val := form.get(field, "").strip():
            payload[field] = val

    if "notes" in form_data:
        payload["notes"] = form.get("notes", "")

    headers: dict[str, str] = {}
    if version := form.get("version", "").strip():
        headers["If-Match"] = version

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/employees/{employee_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            return RedirectResponse(url=f"/employees/{employee_id}", status_code=303)

        errors: dict[str, str] = {}
        try:
            errors["_global"] = resp.json().get("detail") or f"HTTP {resp.status_code}"
        except Exception:
            errors["_global"] = f"HTTP {resp.status_code}"

        contacts = await _fetch_contacts(client)
        super_funds = await _fetch_super_funds(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "employees/edit.html",
        {
            "employee": {"id": str(employee_id), **form},
            "form": form,
            "errors": errors,
            "contacts": contacts,
            "super_funds": super_funds,
        },
    )


# ---------------------------------------------------------------------------
# Terminate
# ---------------------------------------------------------------------------


@router.post(
    "/employees/{employee_id}/terminate",
    response_class=HTMLResponse,
    response_model=None,
)
async def employee_terminate(
    employee_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    end_date = str(form_data.get("end_date", "")).strip()
    reason = str(form_data.get("reason", "")).strip()

    payload = {"end_date": end_date, "reason": reason}

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/employees/{employee_id}/terminate", json=payload
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

    return RedirectResponse(url=f"/employees/{employee_id}", status_code=303)


# ---------------------------------------------------------------------------
# Reveal TFN
# ---------------------------------------------------------------------------


@router.post(
    "/employees/{employee_id}/reveal-tfn",
    response_class=HTMLResponse,
    response_model=None,
)
async def employee_reveal_tfn(
    employee_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    """Call the privileged /tfn endpoint and store the plaintext briefly in
    the session so the detail page can render it once.  The session entry is
    consumed on first load (see detail handler above).
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/employees/{employee_id}/tfn")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            tfn = resp.json().get("tfn", "")
            # Store in session — consumed once by the detail handler
            request.session[f"tfn_reveal_{employee_id}"] = tfn

    return RedirectResponse(url=f"/employees/{employee_id}", status_code=303)


# ---------------------------------------------------------------------------
# Archive (soft-delete)
# ---------------------------------------------------------------------------


@router.post(
    "/employees/{employee_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def employee_archive(
    employee_id: uuid.UUID, request: Request
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.delete(f"/api/v1/employees/{employee_id}")
    return RedirectResponse(url="/employees", status_code=303)
