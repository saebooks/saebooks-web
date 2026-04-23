"""Journal entries list, detail, and create views — Lane D cycles 6 + 16.

GET  /journal-entries           — list page (paginated, HTMX-aware)
GET  /journal-entries/new       — empty create form; two starter lines; no contact
POST /journal-entries/new       — submit to upstream API; redirect on success,
                                  re-render with errors on 422
GET  /journal-entries/_add_line — HTMX partial: returns a single blank debit/credit row
GET  /journal-entries/{id}      — journal entry detail

Route ordering: /journal-entries/new and /journal-entries/_add_line MUST be declared
before /journal-entries/{entry_id} so FastAPI resolves the literal paths first.

API shape (from JournalEntryCreate / JournalLineCreate):
- entry_date : date        (required)
- narration  : str | None  (optional — maps to description in JournalEntryOut)
- reference  : str | None  (optional)
- lines[]    : list[JournalLineCreate]
    account_id : UUID     (required)
    description: str|None
    debit      : Decimal  (default 0)
    credit     : Decimal  (default 0)

No contact_id. No ref on create (auto-assigned JE-000001 etc. by the API).
API enforces debit==credit balance; returns HTTP 422 with a plain string detail
message when the entry is unbalanced.

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

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# JE-specific line fields — debit/credit instead of quantity/unit_price.
_JE_LINE_FIELDS = ("account_id", "description", "debit", "credit")


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _parse_je_lines(form: dict[str, str]) -> list[dict[str, object]]:
    """Extract JE line dicts from a flat form dict.

    Convention: fields are named ``lines[N][field]`` where N is a zero-based
    integer index.  Debit/credit default to "0" when blank (API expects Decimal,
    "0" serialises correctly).  Lines where both debit and credit are "0" and
    account_id is absent are skipped (avoids sending phantom blank rows).
    """
    indices: set[int] = set()
    for key in form:
        if key.startswith("lines[") and "][" in key:
            try:
                idx = int(key.split("[")[1].split("]")[0])
                indices.add(idx)
            except (ValueError, IndexError):
                pass

    lines: list[dict[str, object]] = []
    for idx in sorted(indices):
        line: dict[str, object] = {}
        for field in _JE_LINE_FIELDS:
            val = form.get(f"lines[{idx}][{field}]", "").strip()
            if field in ("debit", "credit"):
                # Always include debit/credit; empty string becomes "0".
                line[field] = val if val else "0"
            elif val:
                line[field] = val
        # Skip if no account selected and both debit and credit are zero.
        if not line.get("account_id") and line.get("debit") == "0" and line.get("credit") == "0":
            continue
        if line:
            lines.append(line)
    return lines


@router.get("/journal-entries", response_class=HTMLResponse, response_model=None)
async def journal_entries_list(
    request: Request,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the journal entries list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``journal_entries/_table.html`` partial only.  Otherwise the full page
    (``journal_entries/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # The API uses page/page_size rather than limit/offset.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    journal_entries: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/journal_entries", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            journal_entries = payload.get("items", [])
            total = payload.get("total", len(journal_entries))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    ctx = {
        "journal_entries": journal_entries,
        "total": total,
        "error": error,
        # Filter values echoed back to the form.
        "filter_status": status or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    # HTMX requests get just the table fragment.
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "journal_entries/_table.html" if is_htmx else "journal_entries/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /{entry_id} so FastAPI matches the
# literal paths first.
# ---------------------------------------------------------------------------


@router.get("/journal-entries/new", response_class=HTMLResponse, response_model=None)
async def journal_entry_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-journal-entry form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates the accounts dropdown (all CoA
    types — JEs can touch any account).

    Two blank starter lines are provided: a debit line and a credit line, so
    the user can immediately enter a balanced entry without clicking 'Add line'.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    accounts: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

    # Two blank starter lines — index 0 (debit) and index 1 (credit).
    initial_lines = [{"index": 0}, {"index": 1}]

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/new.html",
        {
            "form": {"entry_date": today},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
            "lines": initial_lines,
            "line_count": 2,
        },
    )


@router.post("/journal-entries/new", response_class=HTMLResponse, response_model=None)
async def journal_entry_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-journal-entry form.

    Calls POST /api/v1/journal_entries on the upstream API.
    - 201 -> 303 redirect to /journal-entries/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with error; the API returns a plain string detail
             message for balance violations (e.g. "Debits and credits must balance")
    - 401 -> clear session, redirect to /login
    - other errors -> re-render form with a generic error message

    Line-item fields follow the ``lines[N][field]`` naming convention parsed
    by ``_parse_je_lines()``.  Note: the API create field for the entry-level
    summary text is ``narration`` (not ``description`` which is the response alias).
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the top-level payload.
    payload: dict[str, object] = {}
    for field in ("entry_date", "narration", "reference"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["lines"] = _parse_je_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/journal_entries",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/journal-entries/{created['id']}", status_code=303)

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
                # API returns a plain string for balance violations.
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch accounts dropdown for re-render.
    accounts: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

    # Reconstruct lines for re-render from submitted form keys.
    raw_lines = _parse_je_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [
        {"index": 0},
        {"index": 1},
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "accounts": accounts,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get("/journal-entries/_add_line", response_class=HTMLResponse, response_model=None)
async def journal_entry_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank debit/credit line row for the given index.

    Called via hx-get="/journal-entries/_add_line?index=N" to append a new row to
    the line-items table without a full page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "errors": {},
        },
    )


@router.get("/journal-entries/{entry_id}", response_class=HTMLResponse, response_model=None)
async def journal_entry_detail(
    request: Request,
    entry_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single journal entry detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/journal_entries/{entry_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "journal_entries/detail.html",
                {"entry": None, "error": "Journal entry not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "journal_entries/detail.html",
                {"entry": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    entry = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/detail.html",
        {"entry": entry, "error": None},
    )
