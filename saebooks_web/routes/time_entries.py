"""Time-tracking web routes — list / quick-entry / weekly grid / detail.

Killer UX feature: ``/time-entries/week`` — Mon-Sun grid where every
cell is an inline-editable entry. Use it to log hours throughout the
day, then bulk-convert billable rows to a draft invoice.

Routes:

  GET  /time-entries                 — paginated list w/ filters
  GET  /time-entries/new             — quick-add form
  POST /time-entries/new             — submit
  GET  /time-entries/week            — weekly grid (?week_start=YYYY-MM-DD)
  POST /time-entries/week/add        — HTMX: add one entry to a day
  GET  /time-entries/{id}            — detail view
  GET  /time-entries/{id}/edit       — edit form
  POST /time-entries/{id}/edit       — submit edit
  POST /time-entries/{id}/submit     — workflow: DRAFT → SUBMITTED
  POST /time-entries/{id}/approve    — workflow: SUBMITTED → APPROVED
  POST /time-entries/{id}/reject     — workflow: SUBMITTED → REJECTED
  POST /time-entries/{id}/archive    — soft-delete
  POST /time-entries/convert-to-invoice — bulk → invoice line
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
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


def _week_start_for(target: date) -> date:
    """Return the Monday of the week containing ``target``."""
    return target - timedelta(days=target.weekday())


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict]]:
    """Return (contacts, projects) for the entry form."""
    contacts: list[dict] = []
    projects: list[dict] = []
    c_resp = await client.get(
        "/api/v1/contacts", params={"limit": 500, "offset": 0}
    )
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])
    p_resp = await client.get(
        "/api/v1/projects", params={"limit": 500, "offset": 0}
    )
    if p_resp.is_success:
        projects = p_resp.json().get("items", [])
    return contacts, projects


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/time-entries", response_class=HTMLResponse, response_model=None)
async def time_entries_list(
    request: Request,
    project_id: str | None = None,
    contact_id: str | None = None,
    approval_status: str | None = None,
    billable_only: bool = False,
    uninvoiced_only: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if project_id:
        params["project_id"] = project_id
    if contact_id:
        params["contact_id"] = contact_id
    if approval_status:
        params["approval_status"] = approval_status
    if billable_only:
        params["billable_only"] = "true"
    if uninvoiced_only:
        params["uninvoiced_only"] = "true"
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    entries: list[dict] = []
    total = 0
    error: str | None = None
    contacts: list[dict] = []
    projects: list[dict] = []
    async with api_client(request) as client:
        resp = await client.get("/api/v1/time-entries", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            entries = payload.get("items", [])
            total = payload.get("total", len(entries))
        else:
            error = f"API error: HTTP {resp.status_code}"
        contacts, projects = await _fetch_dropdowns(client)

    contact_by_id = {c["id"]: c for c in contacts}
    project_by_id = {p["id"]: p for p in projects}

    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/list.html",
        {
            "entries": entries,
            "total": total,
            "error": error,
            "limit": limit,
            "offset": offset,
            "prev_offset": max(offset - limit, 0) if offset > 0 else None,
            "next_offset": offset + limit if (offset + limit) < total else None,
            "contacts": contacts,
            "projects": projects,
            "contact_by_id": contact_by_id,
            "project_by_id": project_by_id,
            "filter_project_id": project_id or "",
            "filter_contact_id": contact_id or "",
            "filter_approval_status": approval_status or "",
            "filter_billable_only": billable_only,
            "filter_uninvoiced_only": uninvoiced_only,
            "filter_date_from": date_from or "",
            "filter_date_to": date_to or "",
        },
    )


# ---------------------------------------------------------------------------
# New (quick-add form)
# ---------------------------------------------------------------------------


@router.get("/time-entries/new", response_class=HTMLResponse, response_model=None)
async def time_entry_new_form(
    request: Request,
    work_date: str | None = None,
    project_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today_iso = (work_date or date.today().isoformat())
    contacts: list[dict] = []
    projects: list[dict] = []
    async with api_client(request) as client:
        contacts, projects = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/new.html",
        {
            "form": {
                "work_date": today_iso,
                "project_id": project_id or "",
                "hours": "",
                "description": "",
                "billable": False,
                "rate": "",
                "contact_id": "",
            },
            "errors": {},
            "contacts": contacts,
            "projects": projects,
        },
    )


@router.post("/time-entries/new", response_class=HTMLResponse, response_model=None)
async def time_entry_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    payload: dict[str, object] = {
        "work_date": form.get("work_date", "").strip(),
    }
    hours = _parse_decimal(form.get("hours"))
    if hours is not None:
        payload["hours"] = str(hours)
    for field in ("description", "contact_id", "project_id"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val
    if form.get("billable") in ("on", "true", "1"):
        payload["billable"] = True
    rate = _parse_decimal(form.get("rate"))
    if rate is not None:
        payload["rate"] = str(rate)
    break_minutes = form.get("break_minutes", "").strip()
    if break_minutes:
        try:
            payload["break_minutes"] = int(break_minutes)
        except ValueError:
            pass
    for field in ("start_time", "end_time"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    async with api_client(request) as client:
        resp = await client.post("/api/v1/time-entries", json=payload)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code in (200, 201):
            entry_id = resp.json()["id"]
            # If the request came from the weekly grid (HX-Trigger header),
            # bounce back to the grid; else go to detail.
            ref = request.headers.get("Referer", "")
            if "/time-entries/week" in ref:
                return RedirectResponse(
                    url=ref or "/time-entries/week", status_code=303
                )
            return RedirectResponse(
                url=f"/time-entries/{entry_id}", status_code=303
            )
        # Re-render with error.
        errors: dict[str, str] = {}
        try:
            err_body = resp.json()
            errors["_global"] = err_body.get("detail") or f"HTTP {resp.status_code}"
        except Exception:
            errors["_global"] = f"HTTP {resp.status_code}"
        contacts, projects = await _fetch_dropdowns(client)
    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/new.html",
        {
            "form": form,
            "errors": errors,
            "contacts": contacts,
            "projects": projects,
        },
    )


# ---------------------------------------------------------------------------
# Weekly grid (Mon..Sun)
# ---------------------------------------------------------------------------


@router.get("/time-entries/week", response_class=HTMLResponse, response_model=None)
async def time_entry_week(
    request: Request,
    week_start: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    try:
        target = date.fromisoformat(week_start) if week_start else date.today()
    except ValueError:
        target = date.today()
    monday = _week_start_for(target)
    days = [monday + timedelta(days=i) for i in range(7)]

    entries: list[dict] = []
    contacts: list[dict] = []
    projects: list[dict] = []
    error: str | None = None
    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/time-entries/week",
            params={"week_start": monday.isoformat()},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            entries = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"
        contacts, projects = await _fetch_dropdowns(client)

    # Bucket entries by ISO date.
    by_day: dict[str, list[dict]] = {d.isoformat(): [] for d in days}
    for e in entries:
        day = e.get("work_date")
        if day in by_day:
            by_day[day].append(e)

    # Per-day totals.
    day_totals: dict[str, Decimal] = {}
    for d_iso, items in by_day.items():
        total = Decimal("0")
        for e in items:
            try:
                total += Decimal(str(e.get("hours", "0")))
            except (InvalidOperation, TypeError):
                pass
        day_totals[d_iso] = total

    week_total = sum(day_totals.values(), Decimal("0"))
    billable_total = sum(
        (
            Decimal(str(e.get("hours", "0")))
            for items in by_day.values()
            for e in items
            if e.get("billable")
        ),
        Decimal("0"),
    )

    contact_by_id = {c["id"]: c for c in contacts}
    project_by_id = {p["id"]: p for p in projects}

    prev_week = (monday - timedelta(days=7)).isoformat()
    next_week = (monday + timedelta(days=7)).isoformat()
    this_week = _week_start_for(date.today()).isoformat()

    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/week.html",
        {
            "monday": monday.isoformat(),
            "days": [d.isoformat() for d in days],
            "day_labels": [d.strftime("%a") for d in days],
            "day_full_labels": [d.strftime("%a %d %b") for d in days],
            "by_day": by_day,
            "day_totals": {k: f"{v:.2f}" for k, v in day_totals.items()},
            "week_total": f"{week_total:.2f}",
            "billable_total": f"{billable_total:.2f}",
            "non_billable_total": f"{(week_total - billable_total):.2f}",
            "prev_week": prev_week,
            "next_week": next_week,
            "this_week": this_week,
            "contacts": contacts,
            "projects": projects,
            "contact_by_id": contact_by_id,
            "project_by_id": project_by_id,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Detail / edit / workflow
# ---------------------------------------------------------------------------


@router.get("/time-entries/{entry_id}", response_class=HTMLResponse, response_model=None)
async def time_entry_detail(
    entry_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    entry: dict | None = None
    contacts: list[dict] = []
    projects: list[dict] = []
    error: str | None = None
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/time-entries/{entry_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            entry = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"
        contacts, projects = await _fetch_dropdowns(client)

    if entry is None:
        return _TEMPLATES.TemplateResponse(
            request,
            "time_entries/detail.html",
            {"entry": None, "error": error, "contacts": contacts, "projects": projects},
            status_code=404,
        )

    contact_by_id = {c["id"]: c for c in contacts}
    project_by_id = {p["id"]: p for p in projects}

    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/detail.html",
        {
            "entry": entry,
            "error": error,
            "contacts": contacts,
            "projects": projects,
            "contact_by_id": contact_by_id,
            "project_by_id": project_by_id,
        },
    )


@router.get(
    "/time-entries/{entry_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_edit_form(
    entry_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    entry: dict | None = None
    contacts: list[dict] = []
    projects: list[dict] = []
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/time-entries/{entry_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            entry = resp.json()
        contacts, projects = await _fetch_dropdowns(client)

    if entry is None:
        return RedirectResponse(url="/time-entries", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/edit.html",
        {
            "entry": entry,
            "form": entry,  # template branches on form vs entry similarly
            "errors": {},
            "contacts": contacts,
            "projects": projects,
        },
    )


@router.post(
    "/time-entries/{entry_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_edit_submit(
    entry_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    payload: dict[str, object] = {}
    if val := form.get("work_date", "").strip():
        payload["work_date"] = val
    hours = _parse_decimal(form.get("hours"))
    if hours is not None:
        payload["hours"] = str(hours)
    if "description" in form_data:
        payload["description"] = form.get("description", "")
    for field in ("contact_id", "project_id"):
        val = form.get(field, "").strip()
        payload[field] = val or None
    rate = _parse_decimal(form.get("rate"))
    payload["rate"] = str(rate) if rate is not None else None
    payload["billable"] = form.get("billable") in ("on", "true", "1")
    break_minutes = form.get("break_minutes", "").strip()
    if break_minutes:
        try:
            payload["break_minutes"] = int(break_minutes)
        except ValueError:
            pass

    headers = {}
    if version := form.get("version", "").strip():
        headers["If-Match"] = version

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/time-entries/{entry_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            return RedirectResponse(
                url=f"/time-entries/{entry_id}", status_code=303
            )
        contacts, projects = await _fetch_dropdowns(client)
        errors = {}
        try:
            errors["_global"] = resp.json().get("detail") or f"HTTP {resp.status_code}"
        except Exception:
            errors["_global"] = f"HTTP {resp.status_code}"

    # Re-render the edit form.
    return _TEMPLATES.TemplateResponse(
        request,
        "time_entries/edit.html",
        {
            "entry": {"id": str(entry_id), **form},
            "form": form,
            "errors": errors,
            "contacts": [],
            "projects": [],
        },
    )


@router.post(
    "/time-entries/{entry_id}/submit",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_submit(
    entry_id: uuid.UUID, request: Request
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.post(f"/api/v1/time-entries/{entry_id}/submit")
    return RedirectResponse(url=f"/time-entries/{entry_id}", status_code=303)


@router.post(
    "/time-entries/{entry_id}/approve",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_approve(
    entry_id: uuid.UUID, request: Request
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.post(f"/api/v1/time-entries/{entry_id}/approve")
    return RedirectResponse(url=f"/time-entries/{entry_id}", status_code=303)


@router.post(
    "/time-entries/{entry_id}/reject",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_reject(
    entry_id: uuid.UUID, request: Request, reason: str = Form(...)
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.post(
            f"/api/v1/time-entries/{entry_id}/reject",
            json={"reason": reason},
        )
    return RedirectResponse(url=f"/time-entries/{entry_id}", status_code=303)


@router.post(
    "/time-entries/{entry_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_archive(
    entry_id: uuid.UUID, request: Request
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.delete(f"/api/v1/time-entries/{entry_id}")
    return RedirectResponse(url="/time-entries", status_code=303)


# ---------------------------------------------------------------------------
# Convert billable entries → draft invoice line
# ---------------------------------------------------------------------------


@router.post(
    "/time-entries/convert-to-invoice",
    response_class=HTMLResponse,
    response_model=None,
)
async def time_entry_convert(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    entry_ids = form_data.getlist("entry_ids") if hasattr(form_data, "getlist") else []
    if not entry_ids:
        entry_ids = [v for k, v in form_data.multi_items() if k == "entry_ids"]  # type: ignore[attr-defined]

    payload: dict[str, object] = {"entry_ids": entry_ids}
    if invoice_id := form_data.get("invoice_id"):
        payload["invoice_id"] = str(invoice_id)
    if contact_id := form_data.get("contact_id"):
        payload["contact_id"] = str(contact_id)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/time-entries/convert-to-invoice", json=payload
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code in (200, 201):
            body = resp.json()
            return RedirectResponse(
                url=f"/invoices/{body['invoice_id']}", status_code=303
            )
        # Surface the error via flash (session-set) and bounce to list.
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = ""
        request.session["flash"] = (
            f"Convert failed: HTTP {resp.status_code} {detail}"[:300]
        )
    return RedirectResponse(url="/time-entries", status_code=303)
