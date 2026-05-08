"""Cashbook UI — single-entry bookkeeping surfaces.

Routes
------
GET  /cashbook                      — landing page with quick-entry form + recent entries
POST /cashbook/entries              — create entry (form submission)
GET  /cashbook/entries              — full entries list with filters + pagination
GET  /cashbook/entries/{id}/edit    — edit form
POST /cashbook/entries/{id}/edit    — submit PATCH
POST /cashbook/entries/{id}/delete  — delete with If-Match
GET  /cashbook/report               — totals report with date-range picker
GET  /cashbook/report/csv           — CSV export of entries for a date range
GET  /cashbook/upgrade              — upgrade-to-full confirmation page
POST /cashbook/upgrade              — submit upgrade

Auth guard: redirect to /login (303) if no session token.
Cashbook-mode guard: redirect to /dashboard (303) if company.bookkeeping_mode != "cashbook".

API prefix: /api/v1/cashbook  (all calls through api_client helper).
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

logger = logging.getLogger("saebooks_web.cashbook")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Auth / cashbook-mode guards
# ---------------------------------------------------------------------------


def _require_auth(request: Request) -> str | None:
    """Return token if present, else None (caller redirects to /login)."""
    return request.session.get("api_token")


async def _get_active_company(request: Request) -> dict | None:
    """Fetch the first company for the authenticated user. Returns None on error."""
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if resp.is_success:
            payload = resp.json()
            items = payload.get("items", [])
            if items:
                return items[0]
    except Exception:
        pass
    return None


def _today_str() -> str:
    return date.today().isoformat()


def _month_start_str() -> str:
    today = date.today()
    return today.replace(day=1).isoformat()


def _year_start_str() -> str:
    today = date.today()
    return today.replace(month=1, day=1).isoformat()


def _au_fy_start_str() -> str:
    today = date.today()
    if today.month >= 7:
        return date(today.year, 7, 1).isoformat()
    return date(today.year - 1, 7, 1).isoformat()


def _parse_errors(resp_json: dict) -> dict[str, str]:
    """Parse Pydantic-style or string error detail into {field: message}."""
    errors: dict[str, str] = {}
    detail = resp_json.get("detail", [])
    if isinstance(detail, list):
        for err in detail:
            loc = err.get("loc", [])
            field_parts = [p for p in loc if p not in ("body", "query")]
            field = str(field_parts[0]) if field_parts else "__all__"
            errors[field] = err.get("msg", "Invalid value")
    elif isinstance(detail, str):
        errors["__all__"] = detail
    return errors or {"__all__": "Validation error"}


def _gst_implied(amount_str: str) -> str:
    """Return GST component (1/11) of amount, rounded to 2dp, as string."""
    try:
        amt = Decimal(amount_str)
        return str(round(amt / Decimal("11"), 2))
    except (InvalidOperation, TypeError):
        return "0.00"


# ---------------------------------------------------------------------------
# Helper: fetch categories (cached in request.state to avoid double fetch)
# ---------------------------------------------------------------------------


async def _fetch_categories(request: Request) -> list[dict]:
    async with api_client(request) as client:
        resp = await client.get("/api/v1/cashbook/categories")
    if resp.is_success:
        return resp.json() if isinstance(resp.json(), list) else []
    return []


# ---------------------------------------------------------------------------
# /cashbook — landing page
# ---------------------------------------------------------------------------


@router.get("/cashbook", response_class=HTMLResponse, response_model=None)
async def cashbook_landing(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        request.session["flash"] = "This page is for Cashbook companies only."
        return RedirectResponse(url="/", status_code=303)

    today = _today_str()
    month_start = _month_start_str()

    # Fetch categories and recent entries in parallel
    import asyncio

    async def _fetch_entries() -> dict:
        async with api_client(request) as client:
            r = await client.get(
                "/api/v1/cashbook/entries",
                params={"limit": 50, "cursor": None},
            )
        return r.json() if r.is_success else {}

    async def _fetch_summary() -> dict:
        async with api_client(request) as client:
            r = await client.get(
                "/api/v1/cashbook/summary",
                params={"from": month_start, "to": today},
            )
        return r.json() if r.is_success else {}

    categories, entries_payload, summary = await asyncio.gather(
        _fetch_categories(request),
        _fetch_entries(),
        _fetch_summary(),
    )

    entries = entries_payload.get("items", []) if isinstance(entries_payload, dict) else []
    flash = request.session.pop("flash", None)

    # Default direction from session (persists last choice)
    default_direction = request.session.get("cashbook_direction", "income")

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/landing.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "entries": entries,
            "categories": categories,
            "summary": summary,
            "flash": flash,
            "today": today,
            "month_start": month_start,
            "default_direction": default_direction,
            "idempotency_key": str(uuid.uuid4()),
        },
    )


# ---------------------------------------------------------------------------
# POST /cashbook/entries — create (form submit from landing page)
# ---------------------------------------------------------------------------


@router.post("/cashbook/entries", response_class=HTMLResponse, response_model=None)
async def cashbook_entry_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        return RedirectResponse(url="/", status_code=303)

    form_data = await request.form()
    form: dict[str, Any] = {k: v for k, v in form_data.items()}

    idempotency_key = form.get("idempotency_key") or str(uuid.uuid4())
    direction = form.get("direction", "income")
    # Persist direction preference
    request.session["cashbook_direction"] = direction

    amount_str = form.get("amount", "").strip()
    include_gst = form.get("include_gst") == "on"

    payload: dict[str, Any] = {
        "entry_date": form.get("entry_date", _today_str()),
        "direction": direction,
        "amount": amount_str,
        "category_code": form.get("category_code", ""),
    }
    desc = form.get("description", "").strip()
    if desc:
        payload["description"] = desc

    if include_gst and amount_str:
        payload["gst_amount"] = _gst_implied(amount_str)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/cashbook/entries",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        request.session["flash"] = "Entry added."
        return RedirectResponse(url="/cashbook", status_code=303)

    # Error — re-render landing with errors
    errors = _parse_errors(resp.json()) if resp.content else {"__all__": f"API error {resp.status_code}"}

    today = _today_str()
    month_start = _month_start_str()
    import asyncio

    async def _fetch_entries_err() -> dict:
        async with api_client(request) as client:
            r = await client.get("/api/v1/cashbook/entries", params={"limit": 50})
        return r.json() if r.is_success else {}

    async def _fetch_summary_err() -> dict:
        async with api_client(request) as client:
            r = await client.get(
                "/api/v1/cashbook/summary",
                params={"from": month_start, "to": today},
            )
        return r.json() if r.is_success else {}

    categories, entries_payload, summary = await asyncio.gather(
        _fetch_categories(request),
        _fetch_entries_err(),
        _fetch_summary_err(),
    )
    entries = entries_payload.get("items", []) if isinstance(entries_payload, dict) else []

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/landing.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "entries": entries,
            "categories": categories,
            "summary": summary,
            "flash": None,
            "today": today,
            "month_start": month_start,
            "default_direction": direction,
            "idempotency_key": str(uuid.uuid4()),
            "form_errors": errors,
            "form_values": form,
        },
        status_code=422,
    )


# ---------------------------------------------------------------------------
# POST /cashbook/entries/{id}/delete — delete entry
# ---------------------------------------------------------------------------


@router.post(
    "/cashbook/entries/{entry_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_entry_delete(
    request: Request, entry_id: str
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    return_to = str(form_data.get("return_to", "/cashbook"))

    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/cashbook/entries/{entry_id}",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (200, 204):
        request.session["flash"] = "Entry deleted."
    elif resp.status_code == 409:
        request.session["flash"] = "Could not delete — entry was modified elsewhere. Please reload."
    else:
        request.session["flash"] = f"Delete failed (HTTP {resp.status_code})."

    return RedirectResponse(url=return_to, status_code=303)


# ---------------------------------------------------------------------------
# GET /cashbook/entries — full entries list
# ---------------------------------------------------------------------------


@router.get("/cashbook/entries", response_class=HTMLResponse, response_model=None)
async def cashbook_entries_list(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    direction: str | None = None,
    category: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        request.session["flash"] = "This page is for Cashbook companies only."
        return RedirectResponse(url="/", status_code=303)

    # Default date range: this year
    if not from_date:
        from_date = _year_start_str()
    if not to_date:
        to_date = _today_str()

    params: dict[str, Any] = {
        "from": from_date,
        "to": to_date,
        "limit": min(max(limit, 1), 200),
    }
    if direction and direction in ("income", "expense"):
        params["direction"] = direction
    if category:
        params["category"] = category
    if cursor:
        params["cursor"] = cursor

    import asyncio

    async def _fetch_entries_list() -> dict:
        async with api_client(request) as client:
            r = await client.get("/api/v1/cashbook/entries", params=params)
        return r.json() if r.is_success else {"items": [], "next_cursor": None}

    entries_payload, categories = await asyncio.gather(
        _fetch_entries_list(),
        _fetch_categories(request),
    )

    entries = entries_payload.get("items", []) if isinstance(entries_payload, dict) else []
    next_cursor = entries_payload.get("next_cursor") if isinstance(entries_payload, dict) else None
    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/entries_list.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "entries": entries,
            "categories": categories,
            "next_cursor": next_cursor,
            "flash": flash,
            "from_date": from_date,
            "to_date": to_date,
            "filter_direction": direction or "",
            "filter_category": category or "",
            "limit": limit,
        },
    )


# ---------------------------------------------------------------------------
# GET /cashbook/entries/{id}/edit — edit form
# POST /cashbook/entries/{id}/edit — submit PATCH
# NOTE: must appear before any catch-all if we had one.
# ---------------------------------------------------------------------------


@router.get(
    "/cashbook/entries/{entry_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_entry_edit_form(
    request: Request, entry_id: str
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        return RedirectResponse(url="/", status_code=303)

    import asyncio

    async def _fetch_entry() -> dict:
        async with api_client(request) as client:
            r = await client.get(f"/api/v1/cashbook/entries/{entry_id}")
        if r.status_code == 404:
            return {}
        return r.json() if r.is_success else {}

    entry, categories = await asyncio.gather(
        _fetch_entry(),
        _fetch_categories(request),
    )

    if not entry:
        request.session["flash"] = "Entry not found."
        return RedirectResponse(url="/cashbook/entries", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/entry_edit.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "entry": entry,
            "categories": categories,
            "form": entry,
            "errors": {},
            "conflict": False,
        },
    )


@router.post(
    "/cashbook/entries/{entry_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_entry_update(
    request: Request, entry_id: str
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        return RedirectResponse(url="/", status_code=303)

    form_data = await request.form()
    form: dict[str, Any] = {k: v for k, v in form_data.items()}
    version = form.get("version", "")

    amount_str = form.get("amount", "").strip()
    include_gst = form.get("include_gst") == "on"

    payload: dict[str, Any] = {
        "entry_date": form.get("entry_date", _today_str()),
        "direction": form.get("direction", "income"),
        "amount": amount_str,
        "category_code": form.get("category_code", ""),
    }
    desc = form.get("description", "").strip()
    if desc:
        payload["description"] = desc

    if include_gst and amount_str:
        payload["gst_amount"] = _gst_implied(amount_str)
    else:
        payload["gst_amount"] = None

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/cashbook/entries/{entry_id}",
            json=payload,
            headers={"If-Match": str(version)},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Entry updated."
        return RedirectResponse(url="/cashbook/entries", status_code=303)

    categories = await _fetch_categories(request)

    if resp.status_code == 409:
        # Optimistic lock conflict — re-fetch server version
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/cashbook/entries/{entry_id}")
        server_entry = latest_resp.json() if latest_resp.is_success else {}
        return _TEMPLATES.TemplateResponse(
            request,
            "cashbook/entry_edit.html",
            {
                "company": company,
                "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
                "entry": server_entry,
                "categories": categories,
                "form": form,
                "errors": {"__all__": "This entry was edited elsewhere; reload to see latest."},
                "conflict": True,
            },
            status_code=409,
        )

    errors = _parse_errors(resp.json()) if resp.content else {"__all__": f"API error {resp.status_code}"}

    # Re-fetch the entry to populate the form
    async with api_client(request) as client:
        entry_resp = await client.get(f"/api/v1/cashbook/entries/{entry_id}")
    entry = entry_resp.json() if entry_resp.is_success else {}

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/entry_edit.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "entry": entry,
            "categories": categories,
            "form": form,
            "errors": errors,
            "conflict": False,
        },
        status_code=422,
    )


# ---------------------------------------------------------------------------
# GET /cashbook/report — totals report
# ---------------------------------------------------------------------------


@router.get("/cashbook/report", response_class=HTMLResponse, response_model=None)
async def cashbook_report(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    preset: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        request.session["flash"] = "This page is for Cashbook companies only."
        return RedirectResponse(url="/", status_code=303)

    today = date.today()

    # Resolve preset
    if preset == "this_month" or (not from_date and not to_date and not preset):
        from_date = today.replace(day=1).isoformat()
        to_date = today.isoformat()
        active_preset = "this_month"
    elif preset == "this_quarter":
        q_month = ((today.month - 1) // 3) * 3 + 1
        from_date = today.replace(month=q_month, day=1).isoformat()
        to_date = today.isoformat()
        active_preset = "this_quarter"
    elif preset == "this_fy":
        from_date = _au_fy_start_str()
        to_date = today.isoformat()
        active_preset = "this_fy"
    elif preset == "last_fy":
        if today.month >= 7:
            fy_start = date(today.year - 1, 7, 1)
            fy_end = date(today.year, 6, 30)
        else:
            fy_start = date(today.year - 2, 7, 1)
            fy_end = date(today.year - 1, 6, 30)
        from_date = fy_start.isoformat()
        to_date = fy_end.isoformat()
        active_preset = "last_fy"
    else:
        if not from_date:
            from_date = today.replace(day=1).isoformat()
        if not to_date:
            to_date = today.isoformat()
        active_preset = "custom"

    summary: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/cashbook/summary",
            params={"from": from_date, "to": to_date},
        )

    if resp.is_success:
        summary = resp.json()
    else:
        error = f"Could not load report (HTTP {resp.status_code})."

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/report.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "summary": summary,
            "error": error,
            "flash": flash,
            "from_date": from_date,
            "to_date": to_date,
            "active_preset": active_preset,
        },
    )


# ---------------------------------------------------------------------------
# GET /cashbook/report/csv — CSV export
# ---------------------------------------------------------------------------


@router.get("/cashbook/report/csv", response_model=None)
async def cashbook_report_csv(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
) -> StreamingResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = _today_str()
    if not from_date:
        from_date = _month_start_str()
    if not to_date:
        to_date = today

    # Fetch all entries for the period (use a large limit)
    entries: list[dict] = []
    cursor: str | None = None

    async with api_client(request) as client:
        while True:
            params: dict[str, Any] = {
                "from": from_date,
                "to": to_date,
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor

            resp = await client.get("/api/v1/cashbook/entries", params=params)
            if not resp.is_success:
                break
            payload = resp.json()
            batch = payload.get("items", [])
            entries.extend(batch)
            cursor = payload.get("next_cursor")
            if not cursor:
                break

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Date", "Direction", "Category", "Description", "Amount", "GST", "Status", "Ref"]
    )
    for e in entries:
        writer.writerow([
            e.get("entry_date", ""),
            e.get("direction", ""),
            e.get("category_label", ""),
            e.get("description", ""),
            e.get("amount", ""),
            e.get("gst_amount", ""),
            e.get("status", ""),
            e.get("journal_entry_ref", ""),
        ])

    output.seek(0)
    filename = f"cashbook_{from_date}_{to_date}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# GET /cashbook/upgrade — confirmation
# POST /cashbook/upgrade — execute
# ---------------------------------------------------------------------------


@router.get("/cashbook/upgrade", response_class=HTMLResponse, response_model=None)
async def cashbook_upgrade_confirm(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        return RedirectResponse(url="/", status_code=303)

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/upgrade_confirm.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "flash": flash,
            "error": None,
        },
    )


@router.post("/cashbook/upgrade", response_class=HTMLResponse, response_model=None)
async def cashbook_upgrade_submit(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        return RedirectResponse(url="/", status_code=303)

    async with api_client(request) as client:
        resp = await client.post("/api/v1/cashbook/upgrade-to-full")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = (
            "Welcome to full SAE Books. Your cashbook entries are still here "
            "— view them under Banking > Journal Entries."
        )
        return RedirectResponse(url="/", status_code=303)

    # Error
    try:
        detail = resp.json().get("detail") or f"HTTP {resp.status_code}"
    except Exception:
        detail = f"HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/upgrade_confirm.html",
        {
            "company": company,
            "company_name": company.get("legal_name") or company.get("name") or "My Company",
            "bookkeeping_mode": company.get("bookkeeping_mode", "cashbook"),
            "flash": None,
            "error": str(detail),
        },
        status_code=resp.status_code if resp.status_code < 500 else 502,
    )
