"""Bills list, detail, create, and edit views — Lane D cycles 3 + 11 + 13.

GET  /bills              — list page (paginated, HTMX-aware)
GET  /bills/new          — empty create form; generates idempotency key
POST /bills/new          — submit to upstream API; redirect on success,
                           re-render with errors on 422
GET  /bills/_add_line    — HTMX partial: returns a single blank line row
GET  /bills/{id}/edit    — pre-populated edit form (DRAFT only)
POST /bills/{id}/edit    — PATCH to API with If-Match + lines replace
GET  /bills/{id}         — bill detail

Route ordering: /bills/new and /bills/_add_line MUST be declared before
/bills/{bill_id}/edit, which must be declared before /bills/{bill_id}, so
FastAPI resolves the literal paths first.

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.archive_helpers import archive_entity as _archive_entity
from saebooks_web.form_helpers import parse_lines as _parse_lines

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


_BILL_SORT_KEYS = {"number", "issue_date", "due_date", "contact_id", "total", "status"}


def _bill_sort_key(b: dict, key: str) -> object:
    if key == "total":
        try:
            return float(b.get("total") or 0)
        except (TypeError, ValueError):
            return 0.0
    return str(b.get(key) or "")


@router.get("/bills", response_class=HTMLResponse, response_model=None)
async def bills_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "issue_date",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the bills list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``bills/_table.html`` partial only.  Otherwise the full page
    (``bills/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    sort = sort if sort in _BILL_SORT_KEYS else "issue_date"
    direction = "asc" if direction == "asc" else "desc"

    # The API uses page/page_size rather than limit/offset.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if contact_id:
        params["contact_id"] = contact_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    bills: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bills", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            bills = payload.get("items", [])
            total = payload.get("total", len(bills))
        else:
            error = f"API error: HTTP {resp.status_code}"

        # Fetch suppliers so the template can show contact NAME, not the
        # raw UUID. Keep the page-size at 500 — covers all but the largest
        # CoAs, and the list page only renders 50 rows at a time anyway.
        c_resp = await client.get(
            "/api/v1/contacts",
            params={"contact_type": "SUPPLIER", "limit": 500, "offset": 0},
        )
        if c_resp.is_success:
            for c in c_resp.json().get("items", []):
                contacts_by_id[c["id"]] = c

    # Page-level sort. Sorts the current page of results; for global sort
    # across all pages, narrow with filters first.
    bills.sort(key=lambda b: _bill_sort_key(b, sort), reverse=(direction == "desc"))

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    ctx = {
        "bills": bills,
        "total": total,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
        # Filter values echoed back to the form.
        "filter_status": status or "",
        "filter_contact_id": contact_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "sort": sort,
        "direction": direction,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    # HTMX requests get just the table fragment.
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "bills/_table.html" if is_htmx else "bills/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /{bill_id} so FastAPI matches the
# literal paths first.
# ---------------------------------------------------------------------------


@router.get("/bills/new", response_class=HTMLResponse, response_model=None)
async def bill_new_form(
    request: Request,
    contact_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the empty create-bill form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates supplier, expense account and
    tax-code dropdowns from the upstream API.

    When ``contact_id`` is supplied (e.g. from a contact detail page link),
    the supplier is pre-selected and the first line is pre-populated with
    the contact's default_tax_code if one is set.
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
        c_resp = await client.get("/api/v1/contacts", params={"contact_type": "SUPPLIER", "limit": 500, "offset": 0})
        if c_resp.is_success:
            contacts = c_resp.json().get("items", [])

        a_resp = await client.get("/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

        p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
        if p_resp.is_success:
            projects = p_resp.json().get("items", [])

    # Resolve default tax_code_id from the contact's default_tax_code code string.
    default_tax_code_id: str | None = None
    if contact_id:
        async with api_client(request) as client:
            contact_resp = await client.get(f"/api/v1/contacts/{contact_id}")
        if contact_resp.is_success:
            contact_data = contact_resp.json()
            default_code = contact_data.get("default_tax_code")
            if default_code and tax_codes:
                for tc in tax_codes:
                    if tc.get("code") == default_code:
                        default_tax_code_id = str(tc["id"])
                        break

    initial_lines = [{"index": 0, "tax_code_id": default_tax_code_id} if default_tax_code_id else {"index": 0}]

    form: dict[str, object] = {"issue_date": today, "due_date": due}
    if contact_id:
        form["contact_id"] = contact_id

    return _TEMPLATES.TemplateResponse(
        request,
        "bills/new.html",
        {
            "form": form,
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


@router.post("/bills/new", response_class=HTMLResponse, response_model=None)
async def bill_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-bill form.

    Calls POST /api/v1/bills on the upstream API.
    - 201 -> 303 redirect to /bills/{id}  (Post-Redirect-Get)
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
    for field in ("contact_id", "issue_date", "due_date", "number", "notes", "supplier_reference"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    currency = form.get("currency", "").strip().upper() or "AUD"
    payload["currency"] = currency
    fx_rate_raw = form.get("fx_rate", "").strip()
    if fx_rate_raw and currency != "AUD":
        payload["fx_rate"] = fx_rate_raw

    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/bills",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/bills/{created['id']}", status_code=303)

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
        c_resp = await client.get("/api/v1/contacts", params={"contact_type": "SUPPLIER", "limit": 500, "offset": 0})
        if c_resp.is_success:
            contacts = c_resp.json().get("items", [])

        a_resp = await client.get("/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
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
        "bills/new.html",
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


@router.get("/bills/_add_line", response_class=HTMLResponse, response_model=None)
async def bill_add_line(request: Request, index: int = 0) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank line row for the given index.

    Called via hx-get="/bills/_add_line?index=N" to append a new row to the
    line-items table without a full page reload.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

        p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
        if p_resp.is_success:
            projects = p_resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "bills/_line_row.html",
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
# NOTE: these routes MUST appear before /bills/{bill_id} for the same
# literal-vs-parameter ordering reason as /bills/new.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = ("contact_id", "issue_date", "due_date", "notes", "supplier_reference", "currency")

# Statuses that block editing — only DRAFT bills are mutable.
_LOCKED_STATUSES = {"POSTED", "VOIDED"}


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Fetch supplier contacts, expense accounts, tax_codes and projects; return the lists."""
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    c_resp = await client.get("/api/v1/contacts", params={"contact_type": "SUPPLIER", "limit": 500, "offset": 0})
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])

    a_resp = await client.get("/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    p_resp = await client.get("/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0})
    if p_resp.is_success:
        projects = p_resp.json().get("items", [])

    return contacts, accounts, tax_codes, projects


@router.get("/bills/{bill_id}/edit", response_class=HTMLResponse, response_model=None)
async def bill_edit_form(
    request: Request,
    bill_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing bill.

    Only DRAFT bills are editable.  POSTED or VOIDED bills get a read-only
    blocked page instead of the form.

    The current ``version`` is stored in a hidden input so the subsequent
    POST can include it in the ``If-Match`` header for optimistic locking.
    A fresh idempotency key is generated per GET to guard against
    double-submit on page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bills/{bill_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "bills/edit.html",
            {"bill": None, "form": {}, "errors": {"__all__": "Bill not found"},
             "conflict": False, "contacts": [], "accounts": [], "tax_codes": [], "lines": [], "line_count": 0},
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "bills/edit.html",
            {"bill": None, "form": {}, "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
             "conflict": False, "contacts": [], "accounts": [], "tax_codes": [], "lines": [], "line_count": 0},
            status_code=resp.status_code,
        )

    bill = resp.json()

    # Block editing of non-DRAFT bills.
    if bill.get("status") in _LOCKED_STATUSES:
        return _TEMPLATES.TemplateResponse(
            request,
            "bills/edit_blocked.html",
            {"bill": bill},
            status_code=422,
        )

    # Pre-populate the form dict from the API response.
    form: dict[str, object] = {field: bill.get(field) or "" for field in _EDIT_FIELDS}
    form["version"] = str(bill.get("version", ""))
    # fx_rate is numeric — only pre-populate when the bill is foreign-currency.
    stored_rate = bill.get("fx_rate")
    if stored_rate and str(bill.get("currency", "AUD")).upper() != "AUD":
        form["fx_rate"] = str(stored_rate)

    # Build lines list for the form, keyed by zero-based index.
    api_lines = bill.get("lines", [])
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
        "bills/edit.html",
        {
            "bill": bill,
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


@router.post("/bills/{bill_id}/edit", response_class=HTMLResponse, response_model=None)
async def bill_update(
    request: Request,
    bill_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full lines replace.

    Outcomes:
    - 200 OK       -> 303 redirect to /bills/{id}  (Post-Redirect-Get)
    - 409 Conflict -> re-fetch latest record, re-render form with a conflict
                      banner and the server's current version in the hidden
                      input.  The user's submitted values are preserved.
    - 422          -> re-render with per-field validation errors
    - 403          -> flash message on detail page, redirect
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

    # fx_rate is only meaningful for foreign-currency bills.
    currency = str(payload.get("currency", "AUD")).upper()
    fx_rate_raw = form.get("fx_rate", "").strip()
    if fx_rate_raw and currency != "AUD":
        payload["fx_rate"] = fx_rate_raw

    # Lines are always sent (full replace semantics).
    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/bills/{bill_id}",
            json=payload,
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

    if resp.status_code == 403:
        request.session["flash"] = "You do not have permission to edit this bill."
        return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # and show a conflict banner so the user can reconcile.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/bills/{bill_id}")
            server_bill: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_bill.get("version", ""))

            contacts, accounts, tax_codes, projects = await _fetch_dropdowns(client)

        # Preserve user's submitted form values but update the hidden version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        # Reconstruct lines for re-render from submitted values.
        raw_lines = _parse_lines(form)
        lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

        return _TEMPLATES.TemplateResponse(
            request,
            "bills/edit.html",
            {
                "bill": server_bill,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_bill": server_bill,
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
            "PATCH /api/v1/bills/%s returned 428 — If-Match header was missing",
            bill_id,
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
        "bills/edit.html",
        {
            "bill": None,
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
# Archive — POST /{bill_id}/archive
# NOTE: MUST appear before the catch-all /{bill_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/bills/{bill_id}/archive", response_class=HTMLResponse, response_model=None
)
async def bill_archive(
    request: Request,
    bill_id: str,
) -> RedirectResponse:
    """Soft-archive a bill via DELETE /api/v1/bills/{id} with If-Match.

    Only DRAFT bills may be archived; the API returns 422 for POSTED/VOIDED.
    On success redirects to /bills with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/bills",
        entity_id=bill_id,
        version=str(version),
        entity_label=f"Bill {bill_id}",
        list_url="/bills",
        detail_url=f"/bills/{bill_id}",
    )


# ---------------------------------------------------------------------------
# Post transition — POST /bills/{bill_id}/post
# NOTE: MUST appear before the catch-all /{bill_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/bills/{bill_id}/post", response_class=HTMLResponse, response_model=None
)
async def bill_post(
    request: Request,
    bill_id: str,
) -> RedirectResponse:
    """Transition a DRAFT bill to POSTED.

    POSTs to POST /api/v1/bills/{id}/post with If-Match + X-Idempotency-Key.
    - 200 -> 303 to detail with flash "Bill posted."
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
            f"/api/v1/bills/{bill_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Bill posted."
        return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)


# ---------------------------------------------------------------------------
# Void transition — POST /bills/{bill_id}/void
# NOTE: MUST appear before the catch-all /{bill_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/bills/{bill_id}/void", response_class=HTMLResponse, response_model=None
)
async def bill_void(
    request: Request,
    bill_id: str,
) -> RedirectResponse:
    """Transition a POSTED bill to VOIDED.

    POSTs to POST /api/v1/bills/{id}/void with If-Match.
    - 200 -> 303 to detail with flash "Bill voided."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422 -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/bills/{bill_id}/void",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Bill voided."
        return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)


@router.get("/bills/{bill_id}", response_class=HTMLResponse, response_model=None)
async def bill_detail(
    request: Request,
    bill_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single bill detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bills/{bill_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "bills/detail.html",
                {"bill": None, "error": "Bill not found", "flash": None,
                 "attachments": [], "vault_enabled": False},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bills/detail.html",
                {"bill": None, "error": f"API error: HTTP {resp.status_code}", "flash": None,
                 "attachments": [], "vault_enabled": False},
                status_code=resp.status_code,
            )

    bill = resp.json()

    # Fetch attachments for this bill. 503 means vault is disabled — render
    # the panel in its disabled state rather than raising an error.
    attachments: list[dict] = []
    vault_enabled: bool = True
    async with api_client(request) as client:
        att_resp = await client.get(
            "/api/v1/attachments",
            params={"entity_kind": "bill", "entity_id": bill_id},
        )
    if att_resp.status_code == 503:
        vault_enabled = False
    elif att_resp.is_success:
        attachments = att_resp.json()

    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "bills/detail.html",
        {
            "bill": bill,
            "error": None,
            "flash": flash,
            "attachments": attachments,
            "vault_enabled": vault_enabled,
        },
    )
