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
from datetime import date, timedelta
from datetime import date as _date
from datetime import timedelta as _td
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.archive_helpers import archive_entity as _archive_entity
from saebooks_web.form_helpers import parse_lines as _parse_lines_shared

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")

_INVOICE_SORT_KEYS = {"number", "issue_date", "due_date", "contact_id", "total", "status"}

# See [[saebooks-payment-status-filter-pattern]]. The Status column renders
# the computed payment-status badge (templates/_status_macros.html ::
# payment_status_badge), so filter+sort must mirror the badge — not the
# raw DRAFT/POSTED/VOIDED field.
_PAYMENT_STATUS_VALUES = {"draft", "open", "due-soon", "overdue", "partial", "paid", "voided"}

_PAYMENT_STATUS_RAW_HINT = {
    "draft":    "DRAFT",
    "voided":   "VOIDED",
    "open":     "POSTED",
    "due-soon": "POSTED",
    "overdue":  "POSTED",
    "partial":  "POSTED",
    "paid":     "POSTED",
}

_PAYMENT_STATUS_RANK = {
    "draft":    0,
    "overdue":  1,
    "due-soon": 2,
    "open":     3,
    "partial":  4,
    "paid":     5,
    "voided":   6,
}


def _invoice_payment_status(inv: dict, today: str, due_soon_cutoff: str) -> str:
    """Mirror of the Jinja ``payment_status_badge`` macro so filter / sort
    match what the user sees in the Status column."""
    s = (inv.get("status") or "").upper()
    if s == "VOIDED":
        return "voided"
    if s == "DRAFT":
        return "draft"
    if s == "POSTED":
        try:
            total = float(inv.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        try:
            paid = float(inv.get("amount_paid") or 0)
        except (TypeError, ValueError):
            paid = 0.0
        if total > 0 and paid >= total:
            return "paid"
        if paid > 0:
            return "partial"
        due = inv.get("due_date") or ""
        if due and due < today:
            return "overdue"
        if due and due <= due_soon_cutoff:
            return "due-soon"
        return "open"
    return "open"


def _invoice_sort_key(i: dict, key: str, today: str, due_soon_cutoff: str) -> object:
    if key == "total":
        try:
            return float(i.get("total") or 0)
        except (TypeError, ValueError):
            return 0.0
    if key == "status":
        return _PAYMENT_STATUS_RANK.get(
            _invoice_payment_status(i, today, due_soon_cutoff), 99
        )
    return str(i.get(key) or "")


@router.get("/invoices", response_class=HTMLResponse, response_model=None)
async def invoices_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    flagged: bool | None = None,
    sort: str = "issue_date",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the invoices list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``invoices/_table.html`` partial only.  Otherwise the full page
    (``invoices/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    sort = sort if sort in _INVOICE_SORT_KEYS else "issue_date"
    direction = "asc" if direction == "asc" else "desc"

    _today_iso = _date.today().isoformat()
    _due_soon_iso = (_date.today() + _td(days=7)).isoformat()

    # The status filter is a payment-status value (draft / open / due-soon /
    # overdue / partial / paid / voided). For POSTED-derived buckets the
    # API can only narrow to raw status=POSTED — the final filter and the
    # total count are computed in Python below.
    status_norm = (status or "").lower().strip()
    filter_client_side = status_norm in _PAYMENT_STATUS_VALUES
    api_status = _PAYMENT_STATUS_RAW_HINT.get(status_norm) if filter_client_side else None

    # The API uses page/page_size rather than limit/offset.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    # When filtering by payment status we fetch a single wide page from the
    # API (capped at 500 — the API's hard upper bound) and slice in Python
    # so the total count and pagination reflect the filtered set.
    if filter_client_side:
        api_page, api_page_size = 1, 500
    else:
        api_page, api_page_size = page, page_size

    params: dict[str, object] = {"page": api_page, "page_size": api_page_size}
    if api_status:
        params["status"] = api_status
    if contact_id:
        params["contact_id"] = contact_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    # Gap 3 (0157) — "Flagged only" filter. Only forward when explicitly set.
    if flagged:
        params["flagged"] = "true"

    error: str | None = None
    invoices: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/invoices", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            invoices = payload.get("items", [])
            total = payload.get("total", len(invoices))
        else:
            error = f"API error: HTTP {resp.status_code}"

        # Fetch customers so the template can show contact NAME, not the
        # raw UUID. CUSTOMER contact_type covers AR; BOTH lands in either
        # query, so a second pass picks them up.
        for ctype in ("CUSTOMER", "BOTH"):
            c_resp = await client.get(
                "/api/v1/contacts",
                params={"type": ctype, "limit": 500, "offset": 0},
            )
            if c_resp.is_success:
                for c in c_resp.json().get("items", []):
                    contacts_by_id[c["id"]] = c

    if filter_client_side:
        # Drop rows that don't match the requested payment status, then sort
        # and slice in Python so total/pagination reflect the filtered set.
        invoices = [
            i for i in invoices
            if _invoice_payment_status(i, _today_iso, _due_soon_iso) == status_norm
        ]
        invoices.sort(
            key=lambda i: _invoice_sort_key(i, sort, _today_iso, _due_soon_iso),
            reverse=(direction == "desc"),
        )
        total = len(invoices)
        invoices = invoices[offset:offset + limit]
    else:
        # Page-level sort.
        invoices.sort(
            key=lambda i: _invoice_sort_key(i, sort, _today_iso, _due_soon_iso),
            reverse=(direction == "desc"),
        )

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    ctx = {
        "invoices": invoices,
        "total": total,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
        "today": _today_iso,
        "due_soon_cutoff": _due_soon_iso,
        # Filter values echoed back to the form.
        "filter_status": status_norm,
        "filter_contact_id": contact_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "filter_flagged": bool(flagged),
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
    due = (date.today() + timedelta(days=30)).isoformat()

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

_EDIT_FIELDS = ("contact_id", "issue_date", "due_date", "notes", "payment_terms")

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
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

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
# ---------------------------------------------------------------------------


_BULK_ACTIONS = {
    "send":      ("POST", "/api/v1/invoices/{id}/send"),
    "mark_paid": ("POST", "/api/v1/invoices/{id}/mark-paid"),
    "post":      ("POST", "/api/v1/invoices/{id}/post"),
    "void":      ("POST", "/api/v1/invoices/{id}/void"),
    "delete":    ("DELETE", "/api/v1/invoices/{id}"),
}


@router.post("/invoices/bulk", response_class=HTMLResponse, response_model=None)
async def invoices_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many invoices at once.

    Form fields:
      action  — one of: send / mark_paid / post / void / delete
      ids[]   — one entry per invoice UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /invoices. Best-effort: a 409/422 on one row doesn't halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/invoices", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/invoices", status_code=303)

    method, path_tpl = _BULK_ACTIONS[action]
    ok = 0
    failed: list[str] = []
    async with api_client(request) as client:
        for inv_id in ids:
            try:
                resp = await client.request(method, path_tpl.format(id=inv_id))
                if 200 <= resp.status_code < 300:
                    ok += 1
                else:
                    msg = ""
                    try:
                        body = resp.json()
                        detail = body.get("detail")
                        if isinstance(detail, str):
                            msg = detail
                        elif isinstance(detail, list) and detail:
                            msg = detail[0].get("msg", str(detail))
                    except Exception:
                        msg = ""
                    failed.append(f"{inv_id[:8]} ({resp.status_code}{': ' + msg if msg else ''})")
            except Exception as exc:
                failed.append(f"{inv_id[:8]} (transport error: {exc!s})")

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
                 "attachments": [], "vault_enabled": False},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "invoices/detail.html",
                {"invoice": None, "error": f"API error: HTTP {resp.status_code}", "flash": None,
                 "attachments": [], "vault_enabled": False},
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
        },
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
