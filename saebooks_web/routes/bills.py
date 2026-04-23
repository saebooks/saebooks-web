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
from saebooks_web.form_helpers import parse_lines as _parse_lines

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


@router.get("/bills", response_class=HTMLResponse, response_model=None)
async def bills_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
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

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    ctx = {
        "bills": bills,
        "total": total,
        "error": error,
        # Filter values echoed back to the form.
        "filter_status": status or "",
        "filter_contact_id": contact_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
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
async def bill_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-bill form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates supplier, expense account and
    tax-code dropdowns from the upstream API.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()
    due = (date.today() + timedelta(days=30)).isoformat()

    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

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

    # One blank row to start with.
    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "bills/new.html",
        {
            "form": {"issue_date": today, "due_date": due},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
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

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "bills/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "tax_codes": tax_codes,
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match + lines replace)
# NOTE: these routes MUST appear before /bills/{bill_id} for the same
# literal-vs-parameter ordering reason as /bills/new.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = ("contact_id", "issue_date", "due_date", "notes", "supplier_reference")

# Statuses that block editing — only DRAFT bills are mutable.
_LOCKED_STATUSES = {"POSTED", "VOIDED"}


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch supplier contacts, expense accounts and tax_codes; return the lists."""
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

    c_resp = await client.get("/api/v1/contacts", params={"contact_type": "SUPPLIER", "limit": 500, "offset": 0})
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])

    a_resp = await client.get("/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    return contacts, accounts, tax_codes


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
        })
    if not lines:
        lines = [{"index": 0}]

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_dropdowns(client)

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

            contacts, accounts, tax_codes = await _fetch_dropdowns(client)

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

    async with api_client(request) as client:
        contacts2, accounts2, tax_codes2 = await _fetch_dropdowns(client)

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
            "lines": lines2,
            "line_count": len(lines2),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


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
                {"bill": None, "error": "Bill not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bills/detail.html",
                {"bill": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    bill = resp.json()
    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "bills/detail.html",
        {"bill": bill, "error": None, "flash": flash},
    )
