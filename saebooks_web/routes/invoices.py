"""Invoices list, detail, and create views — Lane D cycles 2 + 10.

GET  /invoices              — list page (paginated, HTMX-aware)
GET  /invoices/new          — empty create form; generates idempotency key
POST /invoices/new          — submit to upstream API; redirect on success,
                              re-render with errors on 422
GET  /invoices/_add_line    — HTMX partial: returns a single blank line row
GET  /invoices/{id}         — invoice detail

Route ordering: /invoices/new and /invoices/_add_line MUST be declared before
/invoices/{invoice_id} so FastAPI resolves the literal paths first.

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import uuid
from datetime import date
from datetime import date as _date
from datetime import timedelta as _td
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web import bulk_transitions
from saebooks_web import settlement as settlement_reads
from saebooks_web.routes._void_dating import lodged_conflict, render_lodged_confirm, void_body
from saebooks_web import list_helpers as lh
from saebooks_web.api_client import api_client
from saebooks_web.archive_helpers import archive_entity as _archive_entity
from saebooks_web.form_helpers import parse_lines as _parse_lines_shared
from saebooks_web.fx_context import company_currency
from saebooks_web.invoice_pdf_ee import (
    build_ee_invoice_ctx,
    build_vat_rate_breakdown,
    select_invoice_template,
)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")

def _default_invoice_email_fields(
    invoice: dict, customer: dict, email_settings: dict | None = None
) -> dict:
    """Build default from/to/subject/body for the invoice email composer.

    Shared by the GET compose form (single invoice, user can edit before
    sending) and the bulk-send action (which sends these defaults as-is —
    there is no per-row review UI in a bulk action). b06: the wording,
    signature, sender name, reply-to and From address come from the
    company's email settings — nothing business-specific is built in.
    """
    from saebooks_web import email_defaults as ed

    d = ed.compose("invoice", email_settings or {}, ed.invoice_values(invoice, customer))
    return {
        "from_addr": d["default_from"],
        "from_options": d["from_options"],
        "to": customer.get("email", "") or "",
        "subject": d["subject"],
        "body_html": d["body_html"],
        "reply_to": d["reply_to"],
        "from_name": d["from_name"],
        "bcc": d["bcc"],
        "status": d["status"],
    }


_INVOICE_SORT_KEYS = {
    "number", "issue_date", "due_date", "contact", "total", "amount_due", "status",
}
# Old links said contact_id; the engine sorts by the contact's NAME.
_SORT_ALIASES = {"contact_id": "contact"}

# Status dropdown value -> engine filter. Document states go to ``status``;
# what-is-owed buckets go to ``payment_status`` and are worked out by the
# engine over ALL invoices (the old screen fetched 500 and filtered those).
_STATUS_FILTERS: dict[str, tuple[str, str]] = {
    "draft":    ("status", "DRAFT"),
    "voided":   ("status", "VOIDED"),
    "written-off": ("status", "WRITTEN_OFF"),
    "unpaid":   ("payment_status", "unpaid"),
    "open":     ("payment_status", "open"),
    "due-soon": ("payment_status", "due_soon"),
    "overdue":  ("payment_status", "overdue"),
    "partial":  ("payment_status", "part_paid"),
    "paid":     ("payment_status", "paid"),
}


@router.get("/invoices", response_class=HTMLResponse, response_model=None)
async def invoices_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    flagged: bool | None = None,
    q: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    export: str | None = None,
    sort: str = "issue_date",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse | Response:
    """Render the invoices list page (full or HTMX fragment).

    Searching (``q``: number, customer or an amount), the amount range, the
    status filter, sorting and paging all happen in the engine, so the count,
    the order and the pages cover every invoice. ``?export=csv`` downloads
    every matching invoice, in the same order, as a spreadsheet.

    When the request carries an ``HX-Request: true`` header the response is
    the ``invoices/_table.html`` partial only.  Otherwise the full page
    (``invoices/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    sort = _SORT_ALIASES.get(sort, sort)
    sort = sort if sort in _INVOICE_SORT_KEYS else "issue_date"
    direction = "asc" if direction == "asc" else "desc"
    limit = lh.clamp_page_size(limit)
    offset = max(0, offset)

    _today_iso = _date.today().isoformat()
    _due_soon_iso = (_date.today() + _td(days=7)).isoformat()

    status_norm = (status or "").lower().strip()
    if status_norm not in _STATUS_FILTERS:
        status_norm = ""

    params: dict[str, object] = {
        "page": (offset // limit) + 1,
        "page_size": limit,
        "sort": sort,
        "direction": direction,
    }
    if status_norm:
        key, value = _STATUS_FILTERS[status_norm]
        params[key] = value
    if contact_id:
        params["contact_id"] = contact_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    # Gap 3 (0157) — "Flagged only" filter. Only forward when explicitly set.
    if flagged:
        params["flagged"] = "true"
    search = lh.search_params(q, amount_min, amount_max)
    params.update(search)

    error: str | None = None
    invoices: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}
    filter_contact_name = ""

    async with api_client(request) as client:
        if lh.wants_csv(export):
            resp = await client.get(
                "/api/v1/invoices", params={**params, "format": "csv"}
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                return lh.csv_passthrough(resp, "invoices.csv")
            request.session["flash"] = lh.export_error_message(resp)
            return RedirectResponse(url="/invoices", status_code=303)

        resp = await client.get("/api/v1/invoices", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        list_totals = None
        if resp.is_success:
            payload = resp.json()
            invoices = payload.get("items", [])
            total = payload.get("total", len(invoices))
            list_totals = payload.get("totals")
        else:
            error = f"API error: HTTP {resp.status_code}"

        # Names for exactly the customers on this page (and the filter).
        contacts_by_id = await lh.fetch_contact_names(
            client, [i.get("contact_id") for i in invoices] + [contact_id]
        )
        if contact_id:
            filter_contact_name = str(
                (contacts_by_id.get(contact_id) or {}).get("name") or ""
            )

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    ctx = {
        "invoices": invoices,
        "total": total,
        "list_totals": list_totals,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
        "today": _today_iso,
        "due_soon_cutoff": _due_soon_iso,
        # Filter values echoed back to the form.
        "filter_status": status_norm,
        "filter_contact_id": contact_id or "",
        "filter_contact_name": filter_contact_name,
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "filter_flagged": bool(flagged),
        "filter_q": search.get("q", ""),
        "filter_amount_min": search.get("amount_min", ""),
        "filter_amount_max": search.get("amount_max", ""),
        "sort": sort,
        "direction": direction,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    # HTMX requests get just the table fragment.
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "invoices/_table.html" if is_htmx else "invoices/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /{invoice_id} so FastAPI matches the
# literal paths first.
# ---------------------------------------------------------------------------

def _parse_lines(form: dict[str, str]) -> list[dict[str, object]]:
    """Delegate to the shared helper in form_helpers.py."""
    return _parse_lines_shared(form)


@router.get("/invoices/new", response_class=HTMLResponse, response_model=None)
async def invoice_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-invoice form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates customer, account and tax-code
    dropdowns from the upstream API.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()
    # Blank: filled from the contact's payment terms as soon as one is
    # chosen, or by the engine on save (gap P0-15) — never a flat 30 days.
    due = ""

    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    async with api_client(request) as client:
        contacts = []
        for _ctype in ("CUSTOMER", "BOTH"):
            _r = await client.get(
                "/api/v1/contacts",
                params={"type": _ctype, "limit": 200, "offset": 0},
            )
            if _r.is_success:
                contacts.extend(_r.json().get("items", []))

        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

        p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
        if p_resp.is_success:
            projects = p_resp.json().get("items", [])

    # One blank row to start with.
    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/new.html",
        {
            "form": {"issue_date": today, "due_date": due},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/invoices/new", response_class=HTMLResponse, response_model=None)
async def invoice_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-invoice form.

    Calls POST /api/v1/invoices on the upstream API.
    - 201 -> 303 redirect to /invoices/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with per-field errors + submitted values preserved
    - 401 -> clear session, redirect to /login
    - other errors -> re-render form with a generic error message

    Line-item fields follow the ``lines[N][field]`` naming convention parsed
    by ``_parse_lines()``.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the top-level payload.
    payload: dict[str, object] = {}
    for field in ("contact_id", "issue_date", "due_date", "number", "notes", "payment_terms"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    # One-off customer path: if the form has a one_off_name, create a one-off
    # CUSTOMER contact first and use its id. Failed creation -> re-render with
    # field-level error rather than 500.
    one_off_name = form.get("one_off_name", "").strip()
    if one_off_name and not payload.get("contact_id"):
        async with api_client(request) as _client:
            c_resp = await _client.post(
                "/api/v1/contacts",
                json={
                    "name": one_off_name,
                    "contact_type": "CUSTOMER",
                    "is_one_off": True,
                },
            )
        if c_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if c_resp.status_code == 201:
            payload["contact_id"] = c_resp.json()["id"]
        else:
            errors = {"one_off_name": f"Could not create one-off contact (HTTP {c_resp.status_code})."}
            contacts: list[dict] = []
            accounts: list[dict] = []
            tax_codes: list[dict] = []
            projects: list[dict] = []
            async with api_client(request) as client:
                contacts = []
                for _ctype in ("CUSTOMER", "BOTH"):
                    _r = await client.get(
                        "/api/v1/contacts",
                        params={"type": _ctype, "limit": 500, "offset": 0},
                    )
                    if _r.is_success:
                        contacts.extend(_r.json().get("items", []))
            return _TEMPLATES.TemplateResponse(
                request, "invoices/new.html",
                {"form": form, "errors": errors, "contacts": contacts,
                 "accounts": accounts, "tax_codes": tax_codes,
                 "projects": projects, "idempotency_key": idempotency_key,
                 "lines": [], "line_count": 0},
                status_code=400,
            )

    _apply_currency(request, form, payload)
    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/invoices",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/invoices/{created['id']}", status_code=303)

    # Parse errors for re-render.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field_key = str(field_parts[0]) if field_parts else "__all__"
                    errors[field_key] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch dropdown data for re-render.
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    async with api_client(request) as client:
        contacts = []
        for _ctype in ("CUSTOMER", "BOTH"):
            _r = await client.get(
                "/api/v1/contacts",
                params={"type": _ctype, "limit": 200, "offset": 0},
            )
            if _r.is_success:
                contacts.extend(_r.json().get("items", []))

        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

        p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
        if p_resp.is_success:
            projects = p_resp.json().get("items", [])

    # Reconstruct lines for re-render from submitted form keys.
    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get("/invoices/_add_line", response_class=HTMLResponse, response_model=None)
async def invoice_add_line(request: Request, index: int = 0) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank line row for the given index.

    Called via hx-get="/invoices/_add_line?index=N" to append a new row to the
    line-items table without a full page reload.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

        p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
        if p_resp.is_success:
            projects = p_resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match + lines replace)
# NOTE: these routes MUST appear before /invoices/{invoice_id} for the same
# literal-vs-parameter ordering reason as /invoices/new.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = ("contact_id", "issue_date", "due_date", "notes", "payment_terms", "currency")


def _apply_currency(request: Request, form: Any, payload: dict[str, object]) -> None:
    """Batch 10: currency + rate on an invoice.

    A blank rate on a foreign invoice is left out so the engine looks up
    the published rate for the issue date (or refuses) — never 1.00.
    """
    base_ccy = company_currency(request)
    currency = str(form.get("currency", "") or "").strip().upper() or base_ccy
    payload["currency"] = currency
    fx_rate_raw = str(form.get("fx_rate", "") or "").strip()
    if fx_rate_raw and currency != base_ccy:
        payload["fx_rate"] = fx_rate_raw

# Statuses that block editing. POSTED invoices are mutable — the backend
# regenerates the journal entry in place when lines change (per Richard's
# admin-discretion policy). VOIDED invoices are not editable.
_LOCKED_STATUSES = {"VOIDED"}


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Fetch contacts, accounts, tax_codes and projects; return the lists."""
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    contacts = []
    for _ctype in ("CUSTOMER", "BOTH"):
        _r = await client.get(
            "/api/v1/contacts",
            params={"type": _ctype, "limit": 200, "offset": 0},
        )
        if _r.is_success:
            contacts.extend(_r.json().get("items", []))

    a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
    if p_resp.is_success:
        projects = p_resp.json().get("items", [])

    return contacts, accounts, tax_codes, projects


@router.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse, response_model=None)
async def invoice_edit_form(
    request: Request,
    invoice_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing invoice.

    Only DRAFT invoices are editable.  POSTED or VOIDED invoices get a
    read-only blocked page instead of the form.

    The current ``version`` is stored in a hidden input so the subsequent
    POST can include it in the ``If-Match`` header for optimistic locking.
    A fresh idempotency key is generated per GET to guard against
    double-submit on page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/invoices/{invoice_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/edit.html",
            {"invoice": None, "form": {}, "errors": {"__all__": "Invoice not found"},
             "conflict": False, "contacts": [], "accounts": [], "tax_codes": [], "lines": [], "line_count": 0},
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/edit.html",
            {"invoice": None, "form": {}, "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
             "conflict": False, "contacts": [], "accounts": [], "tax_codes": [], "lines": [], "line_count": 0},
            status_code=resp.status_code,
        )

    invoice = resp.json()

    # Block editing of non-DRAFT invoices.
    if invoice.get("status") in _LOCKED_STATUSES:
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/edit_blocked.html",
            {"invoice": invoice},
            status_code=422,
        )

    # Pre-populate the form dict from the API response.
    form: dict[str, object] = {field: invoice.get(field) or "" for field in _EDIT_FIELDS}
    form["version"] = str(invoice.get("version", ""))
    if str(invoice.get("currency") or "").upper() not in ("", company_currency(request)):
        form["fx_rate"] = str(invoice.get("fx_rate") or "")

    # Build lines list for the form, keyed by zero-based index.
    api_lines = invoice.get("lines", [])
    lines = []
    for i, ln in enumerate(api_lines):
        lines.append({
            "index": i,
            "account_id": str(ln.get("account_id") or ""),
            "description": ln.get("description", ""),
            "quantity": str(ln.get("quantity", "1")),
            "unit_price": str(ln.get("unit_price", "")),
            "tax_code_id": str(ln.get("tax_code_id") or ""),
            "project_id": str(ln.get("project_id") or ""),
        })
    if not lines:
        lines = [{"index": 0}]

    async with api_client(request) as client:
        contacts, accounts, tax_codes, projects = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/edit.html",
        {
            "invoice": invoice,
            "form": form,
            "errors": {},
            "conflict": False,
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": lines,
            "line_count": len(lines),
        },
    )


@router.post("/invoices/{invoice_id}/edit", response_class=HTMLResponse, response_model=None)
async def invoice_update(
    request: Request,
    invoice_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full lines replace.

    Outcomes:
    - 200 OK       -> 303 redirect to /invoices/{id}  (Post-Redirect-Get)
    - 409 Conflict -> re-fetch latest record, re-render form with a conflict
                      banner and the server's current version in the hidden
                      input.  The user's submitted values are preserved.
    - 422          -> re-render with per-field validation errors
    - 403 / POSTED -> flash message on detail page, redirect
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")
    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the PATCH payload — only include non-empty header fields.
    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val
    _apply_currency(request, form, payload)

    # Lines are always sent (full replace semantics).
    payload["lines"] = _parse_lines(form)

    from saebooks_web.features import is_feature_enabled as _ff
    _params = {"force": "true"} if _ff("edit_frozen_state") else None
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/invoices/{invoice_id}",
            json=payload,
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
            params=_params,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    if resp.status_code == 403:
        request.session["flash"] = "You do not have permission to edit this invoice."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # and show a conflict banner so the user can reconcile.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/invoices/{invoice_id}")
            server_invoice: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_invoice.get("version", ""))

            contacts, accounts, tax_codes, projects = await _fetch_dropdowns(client)

        # Preserve user's submitted form values but update the hidden version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        # Reconstruct lines for re-render from submitted values.
        raw_lines = _parse_lines(form)
        lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/edit.html",
            {
                "invoice": server_invoice,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_invoice": server_invoice,
                "idempotency_key": idempotency_key,
                "contacts": contacts,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "projects": projects,
                "lines": lines,
                "line_count": len(lines),
            },
            status_code=409,
        )

    # 422 — parse per-field validation errors.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field_key = str(field_parts[0]) if field_parts else "__all__"
                    errors[field_key] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    elif resp.status_code == 428:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "PATCH /api/v1/invoices/%s returned 428 — If-Match header was missing",
            invoice_id,
        )
        errors["__all__"] = "Precondition required: version information was missing. Please reload and try again."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch dropdowns for re-render.
    contacts2: list[dict] = []
    accounts2: list[dict] = []
    tax_codes2: list[dict] = []
    projects2: list[dict] = []

    async with api_client(request) as client:
        contacts2, accounts2, tax_codes2, projects2 = await _fetch_dropdowns(client)

    raw_lines2 = _parse_lines(form)
    lines2 = [{"index": i, **ln} for i, ln in enumerate(raw_lines2)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/edit.html",
        {
            "invoice": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "idempotency_key": idempotency_key,
            "contacts": contacts2,
            "accounts": accounts2,
            "tax_codes": tax_codes2,
            "projects": projects2,
            "lines": lines2,
            "line_count": len(lines2),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{invoice_id}/archive
# NOTE: MUST appear before the catch-all /{invoice_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/invoices/{invoice_id}/archive", response_class=HTMLResponse, response_model=None
)
async def invoice_archive(
    request: Request,
    invoice_id: str,
) -> RedirectResponse:
    """Soft-archive an invoice via DELETE /api/v1/invoices/{id} with If-Match.

    Only DRAFT invoices may be archived; the API returns 422 for POSTED/VOIDED.
    On success redirects to /invoices with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/invoices",
        entity_id=invoice_id,
        version=str(version),
        entity_label=f"Invoice {invoice_id}",
        list_url="/invoices",
        detail_url=f"/invoices/{invoice_id}",
    )


# ---------------------------------------------------------------------------
# Post transition — POST /invoices/{invoice_id}/post
# NOTE: MUST appear before the catch-all /{invoice_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/invoices/{invoice_id}/post", response_class=HTMLResponse, response_model=None
)
async def invoice_post(
    request: Request,
    invoice_id: str,
) -> RedirectResponse:
    """Transition a DRAFT invoice to POSTED.

    POSTs to POST /api/v1/invoices/{id}/post with If-Match + X-Idempotency-Key.
    - 200 -> 303 to detail with flash "Invoice posted."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422 -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Invoice posted."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)


# ---------------------------------------------------------------------------
# Void transition — POST /invoices/{invoice_id}/void
# NOTE: MUST appear before the catch-all /{invoice_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/invoices/{invoice_id}/void", response_class=HTMLResponse, response_model=None
)
async def invoice_void(
    request: Request,
    invoice_id: str,
) -> RedirectResponse:
    """Transition a POSTED invoice to VOIDED.

    POSTs to POST /api/v1/invoices/{id}/void with If-Match.
    - 200 -> 303 to detail with flash "Invoice voided."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422 -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/void",
            headers={"If-Match": version},
            json=void_body(form_data),
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # Batch 5: the reversal would land in a lodged BAS period — ask first.
    _conflict = lodged_conflict(resp)
    if _conflict is not None:
        return render_lodged_confirm(
            request,
            conflict=_conflict,
            action_url=f"/invoices/{invoice_id}/void",
            back_url=f"/invoices/{invoice_id}",
            version=version,
            what="invoice",
        )

    if resp.status_code == 200:
        request.session["flash"] = "Invoice voided."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)


# ---------------------------------------------------------------------------
# Review flag (Gap 3, 0157) — set/clear via HTMX, swaps the flag control.
# Desired state rides in the query string (?flagged=&compact=), so the POST
# carries no form body and bypasses CSRF Layer 3 (same pattern as the Stripe
# link button). The API enforces auth via the session bearer token.
# ---------------------------------------------------------------------------


@router.post(
    "/invoices/{invoice_id}/review-flag",
    response_class=HTMLResponse,
    response_model=None,
)
async def invoice_review_flag(
    request: Request, invoice_id: str, flagged: bool = True, compact: bool = False
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/review-flag",
            json={"flagged": flagged},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        body = resp.json()
        new_flagged = bool(body.get("flagged_for_review"))
        review_note = body.get("review_note")
    else:
        new_flagged = not flagged
        review_note = None

    return _TEMPLATES.TemplateResponse(
        request,
        "_partials/review_flag.html",
        {
            "flag_base": "/invoices",
            "entity_id": invoice_id,
            "flagged": new_flagged,
            "review_note": review_note,
            "compact": compact,
        },
    )


# ---------------------------------------------------------------------------
# Bulk action endpoint — POST /invoices/bulk
#
# Receives `ids[]` + `action` from the bulk action bar (see
# templates/_components/bulk_action_bar.html). Iterates ids and dispatches
# to the per-row API endpoint. Best-effort: a single failed row doesn't
# abort the batch; per-id outcomes are accumulated in the flash message.
#
# "mark_paid" is deliberately NOT here (and not in the bulk bar / row kebab):
# the engine has no mark-paid endpoint at all, and the correct replacement —
# create_payment — requires a bank_account_id + method the engine can't
# safely default across arbitrary rows. See the "Record payment" per-row
# link in invoices/_table.html and invoices/detail.html, which routes to
# /payments/new prefilled instead.
#
# "send" is handled by _invoices_bulk_send() below, not this generic
# dispatch table — POST /api/v1/invoices/{id}/send-email needs a per-row
# JSON body (to/subject/body) the generic (method, path) shape can't carry,
# and it returns HTTP 200 even when the kill switch blocks the send, so a
# bare status-code check would silently misreport "sent".
# ---------------------------------------------------------------------------


_BULK_ACTIONS = {
    "post":      ("POST", "/api/v1/invoices/{id}/post"),
    "void":      ("POST", "/api/v1/invoices/{id}/void"),
    "delete":    ("DELETE", "/api/v1/invoices/{id}"),
}


async def _invoices_bulk_send(request: Request, ids: list[str]) -> RedirectResponse:
    """Bulk 'send' — email each POSTED invoice using the same default
    template as the single-invoice composer (invoice_email_compose). There
    is no per-row review UI here, so every outcome is reported honestly:
    sent / blocked (kill switch) / skipped (not POSTED, or no customer
    email on file) / failed (anything else, incl. transport errors).

    POST /send-email returns HTTP 200 even when the two-key kill switch
    blocks the send (mode="blocked") — this reads `mode` from the body on
    every row rather than trusting the status code, so a blocked batch is
    never reported as "sent".
    """
    sent = 0
    blocked = 0
    skipped: list[str] = []
    failed: list[str] = []

    from saebooks_web import email_defaults as ed

    async with api_client(request) as client:
        email_settings = await ed.settings_for(client)
        for inv_id in ids:
            iresp = await client.get(f"/api/v1/invoices/{inv_id}")
            if iresp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if not iresp.is_success:
                failed.append(f"{inv_id[:8]} (could not load invoice: HTTP {iresp.status_code})")
                continue
            invoice = iresp.json()
            label = invoice.get("number") or inv_id[:8]

            if invoice.get("status") != "POSTED":
                skipped.append(f"{label} (not posted)")
                continue

            cresp = await client.get(f"/api/v1/contacts/{invoice['contact_id']}")
            customer = cresp.json() if cresp.status_code == 200 else {}
            defaults = _default_invoice_email_fields(invoice, customer, email_settings)

            if not defaults["to"]:
                skipped.append(f"{label} (no customer email on file)")
                continue

            payload = {
                "from_addr": defaults["from_addr"],
                "to": [defaults["to"]],
                "cc": [],
                "bcc": [],
                "subject": defaults["subject"],
                "body_html": defaults["body_html"],
                "reply_to": defaults["reply_to"] or None,
                "from_name": defaults["from_name"] or None,
                "sent_by_user_id": request.session.get("user_id"),
            }
            if defaults["bcc"]:
                payload["bcc"] = [defaults["bcc"]]
            try:
                resp = await client.post(
                    f"/api/v1/invoices/{inv_id}/send-email", json=payload, timeout=120.0
                )
            except Exception as exc:
                failed.append(f"{label} (transport error: {exc!s})")
                continue

            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if not resp.is_success:
                msg = ""
                try:
                    detail = resp.json().get("detail")
                    if isinstance(detail, str):
                        msg = detail
                except Exception:
                    pass
                failed.append(f"{label} ({resp.status_code}{': ' + msg if msg else ''})")
                continue

            result = resp.json()
            mode = result.get("mode", "unknown")
            if mode == "sent":
                sent += 1
            elif mode == "blocked":
                blocked += 1
            else:
                detail = result.get("reason") or result.get("errors") or mode
                failed.append(f"{label} ({mode}: {detail})")

    parts: list[str] = []
    if sent:
        parts.append(f"{sent} sent")
    if blocked:
        parts.append(f"{blocked} blocked (outbound email is currently disabled)")
    if skipped:
        parts.append(
            f"{len(skipped)} skipped — " + "; ".join(skipped[:5])
            + (f" … +{len(skipped) - 5} more" if len(skipped) > 5 else "")
        )
    if failed:
        parts.append(
            f"{len(failed)} failed — " + "; ".join(failed[:5])
            + (f" … +{len(failed) - 5} more" if len(failed) > 5 else "")
        )

    request.session["flash"] = "Send: " + (", ".join(parts) if parts else "no rows processed.")
    return RedirectResponse(url="/invoices", status_code=303)


@router.post("/invoices/bulk", response_class=HTMLResponse, response_model=None)
async def invoices_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many invoices at once.

    Form fields:
      action  — one of: send / post / void / delete
      ids[]   — one entry per invoice UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /invoices. Best-effort: a 409/422 on one row doesn't halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/invoices", status_code=303)

    if action == "send":
        return await _invoices_bulk_send(request, ids)

    if action not in _BULK_ACTIONS:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/invoices", status_code=303)

    method, path_tpl = _BULK_ACTIONS[action]
    # Each row's current version goes with the request (the engine 428s
    # without it) and a row in the wrong state is skipped with a reason —
    # gap P0-12.
    async with api_client(request) as client:
        ok, failed = await bulk_transitions.run_bulk(
            client, ids, method=method, path_tpl=path_tpl, action=action,
        )

    label = action.replace("_", " ").title()
    if failed:
        request.session["flash"] = (
            f"{label}: {ok} succeeded, {len(failed)} failed — " + "; ".join(failed[:5])
            + (f" … +{len(failed) - 5} more" if len(failed) > 5 else "")
        )
    else:
        request.session["flash"] = f"{label}: {ok} invoice{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/invoices", status_code=303)


@router.post(
    "/invoices/{invoice_id}/stripe-payment-link",
    response_class=HTMLResponse,
    response_model=None,
)
async def invoice_stripe_payment_link(
    request: Request,
    invoice_id: str,
) -> HTMLResponse:
    """Generate a Stripe payment link for a POSTED invoice.

    Proxies to POST /api/v1/invoices/{id}/stripe-payment-link.

    Outcomes:
    - 200 -> renders _stripe_payment_link.html with the URL (HTMX swap)
    - 503 -> renders error partial "Stripe not configured"
    - 422 -> renders error partial "Invoice must be posted with outstanding balance"
    """
    if not _require_auth(request):
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/_stripe_payment_link.html",
            {
                "invoice_id": invoice_id,
                "stripe_payment_link": None,
                "stripe_error": "Not authenticated.",
            },
            status_code=403,
        )

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/stripe-payment-link"
        )

    if resp.status_code == 200:
        url = resp.json().get("url", "")
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/_stripe_payment_link.html",
            {
                "invoice_id": invoice_id,
                "stripe_payment_link": url,
                "stripe_error": None,
            },
        )

    if resp.status_code == 503:
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/_stripe_payment_link.html",
            {
                "invoice_id": invoice_id,
                "stripe_payment_link": None,
                "stripe_error": "Stripe not configured — add STRIPE_SECRET_KEY to server config.",
            },
            status_code=200,
        )

    if resp.status_code == 422:
        return _TEMPLATES.TemplateResponse(
            request,
            "invoices/_stripe_payment_link.html",
            {
                "invoice_id": invoice_id,
                "stripe_payment_link": None,
                "stripe_error": "Invoice must be posted with outstanding balance.",
            },
            status_code=200,
        )

    # Generic fallback
    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/_stripe_payment_link.html",
        {
            "invoice_id": invoice_id,
            "stripe_payment_link": None,
            "stripe_error": f"API error: HTTP {resp.status_code}",
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Email an invoice to the customer — GET (composer) + POST (send).
# Mirrors quotes.py's quote_email_compose / quote_email_send: relays to the
# engine's POST /api/v1/invoices/{id}/send-email, whose two-key kill switch
# (SAEBOOKS_EMAIL_SEND_ENABLED env AND tenants.outbound_email_enabled) decides
# sent vs blocked server-side — this route never makes that call itself.
#
# Only POSTED invoices may be sent: the engine does not enforce this on
# send-email, but a DRAFT is not a finalised tax invoice and emailing one
# out would be wrong regardless — matches the POSTED-only precedent already
# set by the Stripe payment link and e-invoice XML routes in this file.
# ---------------------------------------------------------------------------


@router.get("/invoices/{invoice_id}/email", response_class=HTMLResponse, response_model=None)
async def invoice_email_compose(
    request: Request, invoice_id: str
) -> HTMLResponse | RedirectResponse:
    """Email composer for an invoice — pre-fills To/Subject/Body from invoice+contact."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        iresp = await client.get(f"/api/v1/invoices/{invoice_id}")
        if iresp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if iresp.status_code == 404:
            raise HTTPException(404, "Invoice not found")
        if iresp.status_code != 200:
            raise HTTPException(iresp.status_code, "Upstream error")
        invoice = iresp.json()

        cresp = await client.get(f"/api/v1/contacts/{invoice['contact_id']}")
        customer = cresp.json() if cresp.status_code == 200 else {}
        from saebooks_web import email_defaults as ed

        email_settings = await ed.settings_for(client)

    if invoice.get("status") != "POSTED":
        request.session["flash"] = "Invoice must be posted before it can be emailed."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    defaults = _default_invoice_email_fields(invoice, customer, email_settings)

    return _TEMPLATES.TemplateResponse(
        request, "invoices/email_compose.html",
        {
            "invoice":            invoice,
            "form":               {},
            "from_options":       defaults["from_options"],
            "default_from":       defaults["from_addr"],
            "default_reply_to":   defaults["reply_to"],
            "default_from_name":  defaults["from_name"],
            "default_bcc":        defaults["bcc"],
            "email_status":       defaults["status"],
            "default_to":         defaults["to"],
            "default_subject":    defaults["subject"],
            "default_body_html":  defaults["body_html"],
            "flash":              None,
            "flash_kind":         None,
        },
    )


@router.post("/invoices/{invoice_id}/email", response_class=HTMLResponse, response_model=None)
async def invoice_email_send(
    request: Request, invoice_id: str
) -> HTMLResponse | RedirectResponse:
    """Submit the composer — POST to upstream /api/v1/invoices/{id}/send-email.

    Whether it actually sends or gets blocked is decided server-side by the
    two-key kill switch. This route just relays + re-renders the form with
    the result — same contract as quotes.quote_email_send.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form = {k: str(v) for k, v in form_data.items() if k != "csrf_token"}

    to_list  = [s.strip() for s in form.get("to", "").split(",") if s.strip()]
    cc_list  = [s.strip() for s in form.get("cc", "").split(",") if s.strip()]
    bcc_list = [s.strip() for s in form.get("bcc", "").split(",") if s.strip()]

    payload = {
        "from_addr":        form.get("from_addr", ""),
        "to":               to_list,
        "cc":               cc_list,
        "bcc":              bcc_list,
        "subject":          form.get("subject", ""),
        "body_html":        form.get("body_html", ""),
        "reply_to":         form.get("reply_to", "") or None,
        "from_name":        form.get("from_name", "") or None,
        "sent_by_user_id":  request.session.get("user_id"),
    }

    from saebooks_web import email_defaults as ed

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/send-email", json=payload, timeout=120.0
        )
        iresp = await client.get(f"/api/v1/invoices/{invoice_id}")
        invoice = iresp.json() if iresp.status_code == 200 else {}
        email_settings = await ed.settings_for(client)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    defaults = _default_invoice_email_fields(invoice, {}, email_settings)

    result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    flash, flash_kind = ed.send_result_flash(resp, result, to_list)

    return _TEMPLATES.TemplateResponse(
        request, "invoices/email_compose.html",
        {
            "invoice":            invoice,
            "form":               form,
            "from_options":       defaults["from_options"],
            "default_from":       form.get("from_addr") or defaults["from_addr"],
            "default_reply_to":   defaults["reply_to"],
            "default_from_name":  defaults["from_name"],
            "default_bcc":        "",
            "email_status":       defaults["status"],
            "default_to":         "",
            "default_subject":    "",
            "default_body_html":  "",
            "flash":              flash,
            "flash_kind":         flash_kind,
        },
    )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse, response_model=None)
async def invoice_detail(
    request: Request,
    invoice_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single invoice detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/invoices/{invoice_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "invoices/detail.html",
                {"invoice": None, "error": "Invoice not found", "flash": None,
                 "attachments": [], "vault_enabled": False, "email_log": []},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "invoices/detail.html",
                {"invoice": None, "error": f"API error: HTTP {resp.status_code}", "flash": None,
                 "attachments": [], "vault_enabled": False, "email_log": []},
                status_code=resp.status_code,
            )

    invoice = resp.json()

    # Fetch attachments for this invoice. 503 means vault is disabled — render
    # the panel in its disabled state rather than raising an error.
    attachments: list[dict] = []
    vault_enabled: bool = True
    async with api_client(request) as client:
        att_resp = await client.get(
            "/api/v1/attachments",
            params={"entity_kind": "invoice", "entity_id": invoice_id},
        )
    if att_resp.status_code == 503:
        vault_enabled = False
    elif att_resp.is_success:
        attachments = att_resp.json()

    # Customer name, balance due, payments and credits applied (gap P1-2).
    async with api_client(request) as client:
        settlement = await settlement_reads.document_settlement(client, "invoice", invoice_id)

    # Best-effort: pull send history for this invoice — don't fail the page
    # if the email-log endpoint is unavailable (mirrors quotes.quote_detail).
    email_log: list = []
    try:
        async with api_client(request) as client:
            log_resp = await client.get(f"/api/v1/email-log/by-doc/invoice/{invoice_id}")
        if log_resp.status_code == 200:
            email_log = log_resp.json().get("items", [])
    except Exception:
        email_log = []

    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "invoices/detail.html",
        {
            "invoice": invoice,
            "error": None,
            "flash": flash,
            "attachments": attachments,
            "vault_enabled": vault_enabled,
            "email_log": email_log,
            "settlement": settlement,
            "entity_kind": "invoice",
            "entity_id": invoice_id,
        },
    )

# ---------------------------------------------------------------------------
# PDF — Packet 3 (feat/ee-app-surface). Unlike quotes/credit-notes (thin
# proxies to the engine's own /pdf route, which itself POSTs to this app's
# /internal/render/document), the engine's invoice ctx builder has no
# jurisdiction awareness at all today (read against saebooks-m1 branch
# feat/m1-m15-global: template is hardcoded "document" in
# saebooks/api/v1/invoices.py). Template selection is a presentation
# decision — this app owns presentation (see render.py's module docstring)
# — so this route fetches the engine's *facts* (render-context, and for EE
# companies the tax codes needed for a per-rate VAT breakdown), picks the
# template here, and renders in-process via saebooks_web.render. No engine
# change required or made.
# ---------------------------------------------------------------------------


@router.get("/invoices/{invoice_id}/pdf", response_model=None)
async def invoice_pdf(request: Request, invoice_id: str) -> Response | RedirectResponse:
    """Render an invoice PDF — AU ``document`` template by default, the EE
    ``document_ee`` variant for EE companies (see invoice_pdf_ee.py)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        ctx_resp = await client.get(f"/api/v1/invoices/{invoice_id}/render-context")
    if ctx_resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if ctx_resp.status_code == 404:
        raise HTTPException(404, detail="Invoice not found")
    if not ctx_resp.is_success:
        raise HTTPException(ctx_resp.status_code, detail=f"Upstream returned {ctx_resp.status_code}")

    body = ctx_resp.json()
    ctx: dict = dict(body.get("ctx") or {})
    ctx.setdefault("kind", body.get("kind") or "Tax Invoice")

    jurisdiction = getattr(request.state, "active_company_jurisdiction", None) or "AU"
    template_name = select_invoice_template(jurisdiction)

    if template_name == "document_ee":
        async with api_client(request) as client:
            inv_resp = await client.get(f"/api/v1/invoices/{invoice_id}")
            tax_resp = await client.get("/api/v1/tax_codes", params={"limit": 1000})
        # Both calls feed the mandatory VAT-breakdown table (Käibemaksu
        # jaotus määrade kaupa) — a failure here must not fall through to
        # an empty breakdown and a silently-incomplete 200 PDF. Surface it
        # the same way the render-context fetch above does.
        if not inv_resp.is_success:
            raise HTTPException(
                inv_resp.status_code,
                detail=f"Could not load invoice lines for VAT breakdown (upstream {inv_resp.status_code})",
            )
        if not tax_resp.is_success:
            raise HTTPException(
                tax_resp.status_code,
                detail=f"Could not load tax codes for VAT breakdown (upstream {tax_resp.status_code})",
            )
        lines = inv_resp.json().get("lines") or []
        tax_codes_by_id = {
            str(item["id"]): item for item in (tax_resp.json().get("items") or [])
        }
        # Critic round 3: list_tax_codes excludes archived rows (engine's
        # TaxCode.archived_at.is_(None) filter — saebooks/api/v1/tax_codes.py
        # has no include-archived param). A line posted against a code
        # since archived would otherwise fall into build_vat_rate_breakdown's
        # "unclassified 0%" bucket below, silently misreporting its real
        # rate on a statutory VAT document. GET /api/v1/tax_codes/{id} has
        # no archived filter (plain id lookup), so resolve any line's
        # missing tax code that way instead of widening the engine's list
        # filter (engine repo out of scope for this packet).
        missing_ids = {
            str(line["tax_code_id"])
            for line in lines
            if line.get("tax_code_id") and str(line["tax_code_id"]) not in tax_codes_by_id
        }
        if missing_ids:
            async with api_client(request) as client:
                for tax_code_id in missing_ids:
                    tc_resp = await client.get(f"/api/v1/tax_codes/{tax_code_id}")
                    if tc_resp.is_success:
                        item = tc_resp.json()
                        tax_codes_by_id[str(item["id"])] = item
                    elif tc_resp.status_code != 404:
                        # Critic round 4: a genuine 404 means the code truly
                        # doesn't exist for this tenant (get_tax_code's own
                        # not-found case) — falling through to the
                        # unclassified bucket below is the correct, already-
                        # covered behaviour. Anything else (transient 5xx,
                        # timeout-mapped 502/503, RLS/permission edge case)
                        # must not be swallowed the same way — that's the
                        # silently-incomplete-200 outcome inv_resp/tax_resp
                        # above are already careful to avoid.
                        raise HTTPException(
                            tc_resp.status_code,
                            detail=(
                                "Could not resolve tax code for VAT breakdown "
                                f"(upstream {tc_resp.status_code})"
                            ),
                        )
        vat_breakdown = build_vat_rate_breakdown(lines, tax_codes_by_id)
        ctx = build_ee_invoice_ctx(
            ctx,
            vat_breakdown=vat_breakdown,
            # Registrikood convention — see invoice_pdf_ee.py module
            # docstring: no dedicated Company column, same fallback the
            # engine's own einvoice generator uses.
            seller_registration_number=(ctx.get("company") or {}).get("abn"),
            # Contact.registration_number exists on the engine model but
            # ContactOut never exposes it — genuine engine API gap, not
            # fabricated here. Revisit once the engine adds it.
            buyer_registration_number=None,
        )

    import jinja2

    from saebooks_web.render import LatexCompileError, LatexServiceError, render_latex

    try:
        pdf_bytes = await render_latex(template_name, ctx)
    except jinja2.TemplateNotFound as exc:
        # Critic round 4: render_latex() resolves the template via
        # env.get_template() before ever reaching latex-api — a deploy-time
        # drift where document_ee.tex.j2 is absent from the image would
        # otherwise propagate unhandled past this route's own
        # LatexCompileError/LatexServiceError sanitisation, hitting
        # Starlette's default 500 handler instead of the deliberate
        # generic-message pattern used for the other render failure
        # classes below.
        import logging as _logging

        _logging.getLogger(__name__).error(
            "invoice %s pdf render: template %r missing from deployed image: %s",
            invoice_id,
            template_name,
            exc,
        )
        raise HTTPException(500, detail="invoice PDF template unavailable") from exc
    except LatexCompileError as exc:
        # Critic round 3: the compile log tail can contain internal
        # container file paths / template internals. This route is
        # reached directly by an authenticated browser, unlike
        # render.py's internal/render endpoint (server-to-server, bearer
        # token) which returns the same log_tail to a trusted caller —
        # log the detail server-side, return only a generic message here.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "invoice %s pdf render: latex compile failed: %s", invoice_id, exc.log_tail
        )
        raise HTTPException(422, detail="latex compile failed") from exc
    except LatexServiceError as exc:
        # Same reasoning — exc's message embeds the internal latex-api URL
        # (e.g. "http://latex-api:8000") and the raw httpx exception text;
        # matches render.py's own internal/render generic-message pattern
        # for this exception class instead of diverging from it.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "invoice %s pdf render: latex service unavailable: %s", invoice_id, exc
        )
        raise HTTPException(503, detail="latex service unavailable") from exc

    filename = f"invoice-{ctx.get('number') or invoice_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# E-invoice XML — EE (EN 16931 / Peppol BIS 3.0 UBL). Unlike the PDF above
# (presentation the web app owns and renders in-process), the e-invoice XML
# is a statutory compliance artefact the engine owns end-to-end. This route
# is a thin proxy of GET /api/v1/invoices/{id}/einvoice.xml with the session
# bearer (same auth idiom as the PDF path). The engine is the single
# authority on eligibility: it returns 422 with an RFC-7807 problem+json
# body whose ``detail`` explains the refusal (DRAFT, non-EUR, missing seller
# registrikood/VAT, missing buyer country). We surface that ``detail`` to
# the user as a flash — never a bare 500 — and let the engine's own filename
# (invoice-{number}.xml) ride through in Content-Disposition.
# ---------------------------------------------------------------------------


@router.get("/invoices/{invoice_id}/einvoice.xml", response_model=None)
async def invoice_einvoice_xml(
    request: Request, invoice_id: str
) -> Response | RedirectResponse:
    """Proxy the engine's EN 16931 / Peppol BIS 3.0 e-invoice XML download."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/invoices/{invoice_id}/einvoice.xml")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        raise HTTPException(404, detail="Invoice not found")
    if resp.status_code == 422:
        # RFC-7807 problem+json — surface the generator's own message, not a
        # generic error, so the user learns *why* (e.g. "invoice must be
        # posted", "currency must be EUR"). Flash + redirect back to detail.
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        request.session["flash"] = detail or "E-invoice could not be generated."
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)
    if not resp.is_success:
        raise HTTPException(resp.status_code, detail=f"Upstream returned {resp.status_code}")

    # 200 — stream the XML through, preserving the engine's filename header.
    disposition = resp.headers.get(
        "content-disposition", f'inline; filename="invoice-{invoice_id}.xml"'
    )
    return Response(
        content=resp.content,
        media_type="application/xml",
        headers={"Content-Disposition": disposition},
    )


# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def invoice_hard_delete(request: Request, invoice_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/invoices",
        entity_id=invoice_id,
        entity_label=f"Invoice {invoice_id}",
        list_url="/invoices",
        detail_url=f"/invoices/{invoice_id}",
    )
