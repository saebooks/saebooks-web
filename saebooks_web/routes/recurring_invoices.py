"""Recurring invoices list, detail, create, edit, archive, and generate views — Lane D cycles 26 + 30 + 38.

GET  /recurring-invoices            — list page (paginated, HTMX-aware)
GET  /recurring-invoices/new        — empty create form; generates idempotency key
POST /recurring-invoices/new        — submit to upstream API; redirect on success
GET  /recurring-invoices/_add_line  — HTMX partial: returns a single blank line row
GET  /recurring-invoices/{id}/edit  — pre-populated edit form
POST /recurring-invoices/{id}/edit  — PATCH with If-Match; redirect on success
POST /recurring-invoices/{id}/archive — soft-archive via DELETE
POST /recurring-invoices/{id}/pause  — transition ACTIVE -> PAUSED via PATCH status
POST /recurring-invoices/{id}/resume — transition PAUSED -> ACTIVE via PATCH status
POST /recurring-invoices/{id}/generate — generate invoice now; redirect to /invoices/{id} on 201
GET  /recurring-invoices/{id}       — recurring invoice detail

Route ordering: /new + /_add_line + /{id}/edit + /{id}/archive + /{id}/pause + /{id}/resume
+ /{id}/generate MUST be declared before catch-all /{id} so FastAPI resolves literal paths
first.

Auth guard: redirect to /login (303) if no session token.

RecurrenceStatus values: ACTIVE / PAUSED / ENDED.
RecurrenceFrequency values: WEEKLY / FORTNIGHTLY / MONTHLY / QUARTERLY / YEARLY.

The API prefix is /api/v1/recurring_invoices and uses page/page_size pagination.
Status is mutable via PATCH status field (ACTIVE/PAUSED/ENDED transitions).
Archive is terminal and uses DELETE.
Generate calls POST /api/v1/recurring_invoices/{id}/generate.
"""
from __future__ import annotations

import uuid
from datetime import date
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

_FREQUENCIES = ["WEEKLY", "FORTNIGHTLY", "MONTHLY", "QUARTERLY", "YEARLY"]


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _parse_lines(form: dict[str, str]) -> list[dict[str, object]]:
    """Delegate to the shared helper in form_helpers.py."""
    return _parse_lines_shared(form)


# ---------------------------------------------------------------------------
# Shared dropdown fetch helper
# ---------------------------------------------------------------------------


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch contacts (CUSTOMER), accounts and tax_codes in sequence."""
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

    c_resp = await client.get(
        "/api/v1/contacts",
        params={"contact_type": "CUSTOMER", "limit": 200, "offset": 0},
    )
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])

    a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    return contacts, accounts, tax_codes


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/recurring-invoices", response_class=HTMLResponse, response_model=None)
async def recurring_invoices_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the recurring invoices list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``recurring_invoices/_table.html`` partial only.  Otherwise the full
    page (``recurring_invoices/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status

    error: str | None = None
    recurring_invoices: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/recurring_invoices", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            recurring_invoices = payload.get("items", [])
            total = payload.get("total", len(recurring_invoices))
            # Client-side search on name field — API has no free-text search param.
            if search:
                q = search.lower()
                recurring_invoices = [
                    ri for ri in recurring_invoices
                    if q in (ri.get("name") or "").lower()
                ]
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "recurring_invoices": recurring_invoices,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_status": status or "",
        "filter_search": search or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = (
        "recurring_invoices/_table.html" if is_htmx
        else "recurring_invoices/list.html"
    )

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{ri_id} so FastAPI resolves the literal paths first.
# ---------------------------------------------------------------------------


@router.get("/recurring-invoices/new", response_class=HTMLResponse, response_model=None)
async def recurring_invoice_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-recurring-invoice form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates customer, account and tax-code
    dropdowns from the upstream API.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_dropdowns(client)

    # One blank row to start with.
    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_invoices/new.html",
        {
            "form": {"next_run": today, "due_days": "30", "auto_post": False},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": initial_lines,
            "line_count": 1,
            "frequencies": _FREQUENCIES,
        },
    )


@router.post("/recurring-invoices/new", response_class=HTMLResponse, response_model=None)
async def recurring_invoice_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-recurring-invoice form.

    Calls POST /api/v1/recurring_invoices on the upstream API.
    - 201 -> 303 redirect to /recurring-invoices/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with per-field errors + submitted values preserved
    - 401 -> clear session, redirect to /login
    - other errors -> re-render form with a generic error message

    Line-item fields follow the ``lines[N][field]`` naming convention parsed
    by ``_parse_lines()``.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the top-level payload.
    payload: dict[str, object] = {}
    for field in ("contact_id", "name", "frequency", "next_run", "end_date",
                  "payment_terms", "notes"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    # Integer fields — anchor_day and due_days.
    for int_field in ("anchor_day", "due_days"):
        val = form.get(int_field, "").strip()
        if val:
            try:
                payload[int_field] = int(val)
            except ValueError:
                pass

    # Boolean auto_post — checkbox: present -> True, absent -> False.
    payload["auto_post"] = bool(form.get("auto_post"))

    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/recurring_invoices",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/recurring-invoices/{created['id']}", status_code=303)

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
        contacts, accounts, tax_codes = await _fetch_dropdowns(client)

    # Reconstruct lines for re-render from submitted form keys.
    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_invoices/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
            "frequencies": _FREQUENCIES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Add line — HTMX partial
# NOTE: MUST appear before /{ri_id} for literal-vs-parameter ordering.
# ---------------------------------------------------------------------------


@router.get(
    "/recurring-invoices/_add_line", response_class=HTMLResponse, response_model=None
)
async def recurring_invoice_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank line row for the given index.

    Called via hx-get="/recurring-invoices/_add_line?index=N" to append a new
    row to the line-items table without a full page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts: list[dict] = []
    tax_codes: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

        t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_invoices/_line_row.html",
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
# NOTE: MUST appear before /{ri_id} catch-all.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "contact_id", "name", "frequency", "next_run", "end_date",
    "payment_terms", "notes",
)


@router.get(
    "/recurring-invoices/{ri_id}/edit", response_class=HTMLResponse, response_model=None
)
async def recurring_invoice_edit_form(
    request: Request,
    ri_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing recurring invoice.

    Only ACTIVE and PAUSED recurring invoices are editable.
    ENDED invoices get a read-only blocked page.

    The current ``version`` is stored in a hidden input so the subsequent
    POST can include it in the ``If-Match`` header for optimistic locking.
    A fresh idempotency key is generated per GET to guard against
    double-submit on page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/recurring_invoices/{ri_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "recurring_invoices/edit.html",
            {
                "ri": None, "form": {}, "errors": {"__all__": "Recurring invoice not found"},
                "conflict": False, "contacts": [], "accounts": [], "tax_codes": [],
                "lines": [], "line_count": 0, "frequencies": _FREQUENCIES,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "recurring_invoices/edit.html",
            {
                "ri": None, "form": {}, "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False, "contacts": [], "accounts": [], "tax_codes": [],
                "lines": [], "line_count": 0, "frequencies": _FREQUENCIES,
            },
            status_code=resp.status_code,
        )

    ri = resp.json()

    # Block editing of ENDED recurring invoices.
    if ri.get("status") == "ENDED":
        return _TEMPLATES.TemplateResponse(
            request,
            "recurring_invoices/edit_blocked.html",
            {"ri": ri},
            status_code=422,
        )

    # Pre-populate the form dict from the API response.
    form: dict[str, object] = {field: ri.get(field) or "" for field in _EDIT_FIELDS}
    form["version"] = str(ri.get("version", ""))
    form["anchor_day"] = str(ri.get("anchor_day") or "")
    form["due_days"] = str(ri.get("due_days") or "30")
    form["auto_post"] = ri.get("auto_post", False)

    # Build lines list for the form, keyed by zero-based index.
    api_lines = ri.get("lines", [])
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
        "recurring_invoices/edit.html",
        {
            "ri": ri,
            "form": form,
            "errors": {},
            "conflict": False,
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
            "frequencies": _FREQUENCIES,
        },
    )


@router.post(
    "/recurring-invoices/{ri_id}/edit", response_class=HTMLResponse, response_model=None
)
async def recurring_invoice_update(
    request: Request,
    ri_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full lines replace.

    Outcomes:
    - 200 OK       -> 303 redirect to /recurring-invoices/{id}  (Post-Redirect-Get)
    - 409 Conflict -> re-fetch latest record, re-render form with a conflict
                      banner and the server's current version in the hidden input.
    - 422          -> re-render with per-field validation errors
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

    # Integer fields.
    for int_field in ("anchor_day", "due_days"):
        val = form.get(int_field, "").strip()
        if val:
            try:
                payload[int_field] = int(val)
            except ValueError:
                pass

    # Boolean auto_post — checkbox.
    payload["auto_post"] = bool(form.get("auto_post"))

    # Lines are always sent (full replace semantics).
    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/recurring_invoices/{ri_id}",
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
        request.session["flash"] = "Recurring invoice updated."
        return RedirectResponse(url=f"/recurring-invoices/{ri_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # and show a conflict banner so the user can reconcile.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/recurring_invoices/{ri_id}")
            server_ri: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_ri.get("version", ""))

            contacts, accounts, tax_codes = await _fetch_dropdowns(client)

        # Preserve user's submitted form values but update the hidden version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        # Reconstruct lines for re-render from submitted values.
        raw_lines = _parse_lines(form)
        lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

        return _TEMPLATES.TemplateResponse(
            request,
            "recurring_invoices/edit.html",
            {
                "ri": server_ri,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_ri": server_ri,
                "idempotency_key": idempotency_key,
                "contacts": contacts,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "lines": lines,
                "line_count": len(lines),
                "frequencies": _FREQUENCIES,
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
            "PATCH /api/v1/recurring_invoices/%s returned 428 — If-Match header was missing",
            ri_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch dropdowns for re-render.
    async with api_client(request) as client:
        contacts2, accounts2, tax_codes2 = await _fetch_dropdowns(client)

    raw_lines2 = _parse_lines(form)
    lines2 = [{"index": i, **ln} for i, ln in enumerate(raw_lines2)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_invoices/edit.html",
        {
            "ri": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "idempotency_key": idempotency_key,
            "contacts": contacts2,
            "accounts": accounts2,
            "tax_codes": tax_codes2,
            "lines": lines2,
            "line_count": len(lines2),
            "frequencies": _FREQUENCIES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{ri_id}/archive
# NOTE: MUST appear before catch-all /{ri_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/recurring-invoices/{ri_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def recurring_invoice_archive(
    request: Request,
    ri_id: str,
) -> RedirectResponse:
    """Soft-archive a recurring invoice via DELETE /api/v1/recurring_invoices/{id}
    with If-Match.

    On success redirects to /recurring-invoices with a flash.
    On 409 or 422 redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/recurring_invoices",
        entity_id=ri_id,
        version=str(version),
        entity_label=f"Recurring invoice {ri_id}",
        list_url="/recurring-invoices",
        detail_url=f"/recurring-invoices/{ri_id}",
    )


# ---------------------------------------------------------------------------
# Status transitions — Pause + Resume via PATCH status
# NOTE: MUST appear before catch-all /{ri_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/recurring-invoices/{ri_id}/pause",
    response_class=HTMLResponse,
    response_model=None,
)
async def recurring_invoice_pause(
    request: Request,
    ri_id: str,
) -> RedirectResponse:
    """Transition ACTIVE -> PAUSED via PATCH status field.

    Reads version from form for optimistic locking.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/recurring_invoices/{ri_id}",
            json={"status": "PAUSED"},
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Recurring invoice paused."
    elif resp.status_code == 409:
        request.session["flash"] = "Version conflict — refresh and try again."
    else:
        try:
            detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
            if isinstance(detail, list) and detail:
                detail = detail[0].get("msg", str(detail))
        except Exception:
            detail = f"API error: HTTP {resp.status_code}"
        request.session["flash"] = str(detail)

    return RedirectResponse(url=f"/recurring-invoices/{ri_id}", status_code=303)


@router.post(
    "/recurring-invoices/{ri_id}/resume",
    response_class=HTMLResponse,
    response_model=None,
)
async def recurring_invoice_resume(
    request: Request,
    ri_id: str,
) -> RedirectResponse:
    """Transition PAUSED -> ACTIVE via PATCH status field.

    Reads version from form for optimistic locking.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/recurring_invoices/{ri_id}",
            json={"status": "ACTIVE"},
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Recurring invoice resumed."
    elif resp.status_code == 409:
        request.session["flash"] = "Version conflict — refresh and try again."
    else:
        try:
            detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
            if isinstance(detail, list) and detail:
                detail = detail[0].get("msg", str(detail))
        except Exception:
            detail = f"API error: HTTP {resp.status_code}"
        request.session["flash"] = str(detail)

    return RedirectResponse(url=f"/recurring-invoices/{ri_id}", status_code=303)


# ---------------------------------------------------------------------------
# Generate — POST /{ri_id}/generate
# NOTE: MUST appear before catch-all /{ri_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/recurring-invoices/{ri_id}/generate",
    response_class=HTMLResponse,
    response_model=None,
)
async def recurring_invoice_generate(
    request: Request,
    ri_id: str,
) -> RedirectResponse:
    """Generate an invoice immediately from an ACTIVE recurring invoice.

    Reads version and idempotency_key from the form for optimistic locking and
    deduplication.

    Outcomes:
    - 201 Created -> 303 redirect to /invoices/{invoice_id}
    - 409 Conflict -> flash "version conflict" + 303 back to RI detail
    - 422 Unprocessable -> flash API error detail + 303 back to RI detail
    - other errors -> flash generic error + 303 back to RI detail
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(form_data.get("idempotency_key", str(uuid.uuid4())))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/recurring_invoices/{ri_id}/generate",
            json={"version": int(version)} if version.isdigit() else {},
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        data = resp.json()
        invoice_id = data.get("invoice_id") or data.get("id")
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — refresh and try again."
        return RedirectResponse(url=f"/recurring-invoices/{ri_id}", status_code=303)

    # 422 or other errors — extract detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"

    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/recurring-invoices/{ri_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail — catch-all /{ri_id}
# NOTE: MUST be last so the literal paths above take precedence.
# ---------------------------------------------------------------------------


@router.get("/recurring-invoices/{ri_id}", response_class=HTMLResponse, response_model=None)
async def recurring_invoice_detail(
    request: Request,
    ri_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single recurring invoice detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/recurring_invoices/{ri_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "recurring_invoices/detail.html",
                {"ri": None, "error": "Recurring invoice not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "recurring_invoices/detail.html",
                {"ri": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    ri = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_invoices/detail.html",
        {
            "ri": ri,
            "error": None,
            "flash": flash,
            "generate_idempotency_key": str(uuid.uuid4()),
        },
    )
