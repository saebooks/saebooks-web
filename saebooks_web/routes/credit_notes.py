"""Credit notes list, detail, create, and edit views — Lane D cycles 5 + 14 + 15 + 39.

GET  /credit-notes               — list page (paginated, HTMX-aware)
GET  /credit-notes/new           — empty create form; generates idempotency key
POST /credit-notes/new           — submit to upstream API; redirect on success,
                                   re-render with errors on 422
GET  /credit-notes/_add_line     — HTMX partial: returns a single blank line row
GET  /credit-notes/{id}/edit     — pre-populated edit form (DRAFT only)
POST /credit-notes/{id}/edit     — PATCH to API with If-Match + lines replace
POST /credit-notes/{id}/post     — transition DRAFT -> POSTED
POST /credit-notes/{id}/void     — transition POSTED -> VOIDED
GET  /credit-notes/{id}          — credit note detail

Route ordering: /credit-notes/new and /credit-notes/_add_line MUST be declared
before /credit-notes/{credit_note_id}/edit, which must be declared before
/credit-notes/{credit_note_id}/post and /credit-notes/{credit_note_id}/void,
which must be declared before /credit-notes/{credit_note_id},
so FastAPI resolves the literal paths first.

Divergences from invoices pattern:
- URL slug is hyphenated (/credit-notes) but API path uses underscores (/api/v1/credit_notes).
- No currency field in CreditNoteOut — omit currency display, show bare amounts.
- Has original_invoice_id (nullable) — "Applied to" section links to /invoices/{id} if set.
- Has amount_allocated (Decimal) — partial application tracking.
- Has reason (nullable str) — shown in detail header.
- CreditNoteCreate/Update has NO due_date, NO number, NO payment_terms — only contact_id,
  issue_date, reason, notes, original_invoice_id, and lines.
- The void endpoint returns 204 No Content (not 200) — handler treats 204 as success.

Auth guard: redirect to /login (303) if no session token.
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
from saebooks_web.form_helpers import parse_lines as _parse_lines

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


@router.get("/credit-notes", response_class=HTMLResponse, response_model=None)
async def credit_notes_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the credit notes list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``credit_notes/_table.html`` partial only.  Otherwise the full page
    (``credit_notes/list.html``) is returned.
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
    credit_notes: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/credit_notes", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            credit_notes = payload.get("items", [])
            total = payload.get("total", len(credit_notes))
        else:
            error = f"API error: HTTP {resp.status_code}"

        # Resolve contact names — credit notes can go to either side
        # (customer refund or supplier credit), so pull both pools.
        for ctype in ("CUSTOMER", "SUPPLIER", "BOTH"):
            c_resp = await client.get(
                "/api/v1/contacts",
                params={"type": ctype, "limit": 500, "offset": 0},
            )
            if c_resp.is_success:
                for c in c_resp.json().get("items", []):
                    contacts_by_id[c["id"]] = c

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    ctx = {
        "credit_notes": credit_notes,
        "total": total,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
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
    template = "credit_notes/_table.html" if is_htmx else "credit_notes/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /{credit_note_id} so FastAPI matches
# the literal paths first.
# ---------------------------------------------------------------------------


@router.get("/credit-notes/new", response_class=HTMLResponse, response_model=None)
async def credit_note_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-credit-note form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates customer, account and tax-code
    dropdowns from the upstream API.

    CreditNoteCreate has no due_date or number — only contact_id, issue_date,
    reason, notes, original_invoice_id, and lines.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

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

    # One blank row to start with.
    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "credit_notes/new.html",
        {
            "form": {"issue_date": today},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/credit-notes/new", response_class=HTMLResponse, response_model=None)
async def credit_note_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-credit-note form.

    Calls POST /api/v1/credit_notes on the upstream API.
    - 201 -> 303 redirect to /credit-notes/{id}  (Post-Redirect-Get)
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

    # Build the top-level payload — only non-empty optional fields included.
    payload: dict[str, object] = {}
    for field in ("contact_id", "issue_date", "reason", "notes", "original_invoice_id"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/credit_notes",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/credit-notes/{created['id']}", status_code=303)

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

    # Reconstruct lines for re-render from submitted form keys.
    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "credit_notes/new.html",
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


@router.get("/credit-notes/_add_line", response_class=HTMLResponse, response_model=None)
async def credit_note_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank line row for the given index.

    Called via hx-get="/credit-notes/_add_line?index=N" to append a new row to
    the line-items table without a full page reload.
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
        "credit_notes/_line_row.html",
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
# NOTE: these routes MUST appear before /credit-notes/{credit_note_id} for the
# same literal-vs-parameter ordering reason as /credit-notes/new.
# ---------------------------------------------------------------------------

# CreditNoteUpdate mutable fields — no due_date, no payment_terms, no number.
_EDIT_FIELDS = ("contact_id", "issue_date", "reason", "notes", "original_invoice_id")

# Statuses that block editing — only DRAFT credit notes are mutable.
_LOCKED_STATUSES = {"POSTED", "VOIDED"}


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch customer contacts, accounts and tax_codes; return the lists."""
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

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

    return contacts, accounts, tax_codes


@router.get("/credit-notes/{credit_note_id}/edit", response_class=HTMLResponse, response_model=None)
async def credit_note_edit_form(
    request: Request,
    credit_note_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing credit note.

    Only DRAFT credit notes are editable.  POSTED or VOIDED credit notes get a
    read-only blocked page instead of the form.

    The current ``version`` is stored in a hidden input so the subsequent POST
    can include it in the ``If-Match`` header for optimistic locking.  A fresh
    idempotency key is generated per GET to guard against double-submit on
    page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/credit_notes/{credit_note_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "credit_notes/edit.html",
            {
                "credit_note": None, "form": {},
                "errors": {"__all__": "Credit note not found"},
                "conflict": False, "contacts": [], "accounts": [],
                "tax_codes": [], "lines": [], "line_count": 0,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "credit_notes/edit.html",
            {
                "credit_note": None, "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False, "contacts": [], "accounts": [],
                "tax_codes": [], "lines": [], "line_count": 0,
            },
            status_code=resp.status_code,
        )

    credit_note = resp.json()

    # Block editing of non-DRAFT credit notes.
    if credit_note.get("status") in _LOCKED_STATUSES:
        return _TEMPLATES.TemplateResponse(
            request,
            "credit_notes/edit_blocked.html",
            {"credit_note": credit_note},
            status_code=422,
        )

    # Pre-populate the form dict from the API response.
    form: dict[str, object] = {field: credit_note.get(field) or "" for field in _EDIT_FIELDS}
    form["version"] = str(credit_note.get("version", ""))

    # Build lines list for the form, keyed by zero-based index.
    api_lines = credit_note.get("lines", [])
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
        "credit_notes/edit.html",
        {
            "credit_note": credit_note,
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


@router.post("/credit-notes/{credit_note_id}/edit", response_class=HTMLResponse, response_model=None)
async def credit_note_update(
    request: Request,
    credit_note_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full lines replace.

    Outcomes:
    - 200 OK       -> 303 redirect to /credit-notes/{id}  (Post-Redirect-Get)
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

    from saebooks_web.features import is_feature_enabled as _ff
    _params = {"force": "true"} if _ff("edit_frozen_state") else None
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/credit_notes/{credit_note_id}",
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
        return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)

    if resp.status_code == 403:
        request.session["flash"] = "You do not have permission to edit this credit note."
        return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # and show a conflict banner so the user can reconcile.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/credit_notes/{credit_note_id}")
            server_credit_note: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_credit_note.get("version", ""))

            contacts, accounts, tax_codes = await _fetch_dropdowns(client)

        # Preserve user's submitted form values but update the hidden version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        # Reconstruct lines for re-render from submitted values.
        raw_lines = _parse_lines(form)
        lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

        return _TEMPLATES.TemplateResponse(
            request,
            "credit_notes/edit.html",
            {
                "credit_note": server_credit_note,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_credit_note": server_credit_note,
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
            "PATCH /api/v1/credit_notes/%s returned 428 — If-Match header was missing",
            credit_note_id,
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
        "credit_notes/edit.html",
        {
            "credit_note": None,
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


# ---------------------------------------------------------------------------
# Post transition — POST /{credit_note_id}/post
# NOTE: MUST appear before the catch-all /{credit_note_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/credit-notes/{credit_note_id}/post",
    response_class=HTMLResponse,
    response_model=None,
)
async def credit_note_post(
    request: Request,
    credit_note_id: str,
) -> RedirectResponse:
    """Transition a DRAFT credit note to POSTED.

    POSTs to POST /api/v1/credit_notes/{id}/post with If-Match + X-Idempotency-Key.
    - 200 -> 303 to detail with flash "Credit note posted."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422/other -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/credit_notes/{credit_note_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Credit note posted."
        return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)


# ---------------------------------------------------------------------------
# Void transition — POST /{credit_note_id}/void
# NOTE: MUST appear before the catch-all /{credit_note_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/credit-notes/{credit_note_id}/void",
    response_class=HTMLResponse,
    response_model=None,
)
async def credit_note_void(
    request: Request,
    credit_note_id: str,
) -> RedirectResponse:
    """Transition a POSTED credit note to VOIDED.

    POSTs to POST /api/v1/credit_notes/{id}/void with If-Match.
    - 204 -> 303 to detail with flash "Credit note voided."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422/other -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/credit_notes/{credit_note_id}/void",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 204:
        request.session["flash"] = "Credit note voided."
        return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)


# ---------------------------------------------------------------------------
# Archive — POST /{credit_note_id}/archive
# NOTE: MUST appear before the catch-all /{credit_note_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/credit-notes/{credit_note_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def credit_note_archive(
    request: Request,
    credit_note_id: str,
) -> RedirectResponse:
    """Soft-archive a credit note via DELETE /api/v1/credit_notes/{id} with If-Match.

    Only DRAFT credit notes may be archived; the API returns 422 for POSTED/VOIDED.
    On success redirects to /credit-notes with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/credit_notes",
        entity_id=credit_note_id,
        version=str(version),
        entity_label=f"Credit note {credit_note_id}",
        list_url="/credit-notes",
        detail_url=f"/credit-notes/{credit_note_id}",
    )


@router.get("/credit-notes/{credit_note_id}", response_class=HTMLResponse, response_model=None)
async def credit_note_detail(
    request: Request,
    credit_note_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single credit note detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/credit_notes/{credit_note_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "credit_notes/detail.html",
                {"credit_note": None, "error": "Credit note not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "credit_notes/detail.html",
                {"credit_note": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    credit_note = resp.json()
    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "credit_notes/detail.html",
        {"credit_note": credit_note, "error": None, "flash": flash},
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /credit-notes/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_CREDIT_NOTES = {
    "post": ("POST", "/api/v1/credit_notes/{id}/post"),
    "void": ("POST", "/api/v1/credit_notes/{id}/void"),
    "archive": ("DELETE", "/api/v1/credit_notes/{id}"),
}


@router.post("/credit-notes/bulk", response_class=HTMLResponse, response_model=None)
async def credit_notes_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many credit notes at once.

    Form fields:
      action  — one of: post, void, archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /credit-notes. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_CREDIT_NOTES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/credit-notes", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/credit-notes", status_code=303)

    method, path_tpl = _BULK_ACTIONS_CREDIT_NOTES[action]
    ok = 0
    failed: list[str] = []
    async with api_client(request) as client:
        for row_id in ids:
            try:
                resp = await client.request(method, path_tpl.format(id=row_id))
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
                    failed.append(f"{row_id[:8]} ({resp.status_code}{': ' + msg if msg else ''})")
            except Exception as exc:
                failed.append(f"{row_id[:8]} (transport error: {exc!s})")

    label = action.replace("_", " ").title()
    if failed:
        request.session["flash"] = (
            f"{label}: {ok} succeeded, {len(failed)} failed — " + "; ".join(failed[:5])
            + (f" … +{len(failed) - 5} more" if len(failed) > 5 else "")
        )
    else:
        request.session["flash"] = f"{label}: {ok} credit note{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/credit-notes", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/credit-notes/{cn_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def credit_note_hard_delete(request: Request, cn_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/credit_notes",
        entity_id=cn_id,
        entity_label=f"Credit note {cn_id}",
        list_url="/credit-notes",
        detail_url=f"/credit-notes/{cn_id}",
    )
