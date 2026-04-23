"""Credit notes list, detail, and create views — Lane D cycles 5 + 14.

GET  /credit-notes           — list page (paginated, HTMX-aware)
GET  /credit-notes/new       — empty create form; generates idempotency key
POST /credit-notes/new       — submit to upstream API; redirect on success,
                               re-render with errors on 422
GET  /credit-notes/_add_line — HTMX partial: returns a single blank line row
GET  /credit-notes/{id}      — credit note detail

Route ordering: /credit-notes/new and /credit-notes/_add_line MUST be declared
before /credit-notes/{credit_note_id} so FastAPI resolves the literal paths first.

Divergences from invoices pattern:
- URL slug is hyphenated (/credit-notes) but API path uses underscores (/api/v1/credit_notes).
- No currency field in CreditNoteOut — omit currency display, show bare amounts.
- Has original_invoice_id (nullable) — "Applied to" section links to /invoices/{id} if set.
- Has amount_allocated (Decimal) — partial application tracking.
- Has reason (nullable str) — shown in detail header.
- CreditNoteCreate has NO due_date and NO number — only contact_id, issue_date, reason,
  notes, original_invoice_id, and lines.

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

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    ctx = {
        "credit_notes": credit_notes,
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
                {"credit_note": None, "error": "Credit note not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "credit_notes/detail.html",
                {"credit_note": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    credit_note = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "credit_notes/detail.html",
        {"credit_note": credit_note, "error": None},
    )
