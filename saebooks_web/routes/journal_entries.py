"""Journal entries list, detail, create, and edit views — Lane D cycles 6 + 16 + 17 + 37.

GET  /journal-entries              — list page (paginated, HTMX-aware)
GET  /journal-entries/new          — empty create form; two starter lines; no contact
POST /journal-entries/new          — submit to upstream API; redirect on success,
                                     re-render with errors on 422
GET  /journal-entries/_add_line    — HTMX partial: returns a single blank debit/credit row
GET  /journal-entries/{id}/edit    — pre-populated edit form (DRAFT only)
POST /journal-entries/{id}/edit    — PATCH to API with If-Match + lines replace
POST /journal-entries/{id}/post    — transition DRAFT -> POSTED via dedicated endpoint
POST /journal-entries/{id}/reverse — transition POSTED -> REVERSED; redirect to reversal entry
GET  /journal-entries/{id}         — journal entry detail

Route ordering: /journal-entries/new and /journal-entries/_add_line MUST be declared
before /journal-entries/{entry_id}/edit, which must be declared before
/journal-entries/{entry_id}/post and /journal-entries/{entry_id}/reverse (all literal
sub-paths), which must be declared before /journal-entries/{entry_id}, so FastAPI
resolves the literal paths first.

API shape (from JournalEntryCreate / JournalEntryUpdate / JournalLineCreate):
- entry_date : date        (required on create; optional on update)
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

JournalEntryUpdate also exposes `status` but status transitions (DRAFT→POSTED→REVERSED)
are performed via dedicated post/reverse endpoints — `status` is intentionally
excluded from _EDIT_FIELDS so the edit form cannot transition status.

Locked statuses: POSTED + REVERSED both block editing; only DRAFT is mutable.

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

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# JE-specific line fields — debit/credit instead of quantity/unit_price.
_JE_LINE_FIELDS = ("account_id", "description", "debit", "credit", "tax_code_id")


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

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    ctx = {
        "journal_entries": journal_entries,
        "total": total,
        "error": error,
        "flash": flash,
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
    tax_codes: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])
        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

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
            "tax_codes": tax_codes,
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

    # Re-fetch accounts and tax_codes dropdowns for re-render.
    accounts: list[dict] = []
    tax_codes: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])
        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

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
            "tax_codes": tax_codes,
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
    tax_codes: list[dict] = []

    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])
        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/_line_row.html",
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
# NOTE: these routes MUST appear before /journal-entries/{entry_id} for the
# same literal-vs-parameter ordering reason as /journal-entries/new.
# ---------------------------------------------------------------------------

# JournalEntryUpdate mutable header fields — status excluded (use post/reverse endpoints).
_EDIT_FIELDS = ("entry_date", "narration", "reference")

# Statuses that block editing — only DRAFT journal entries are mutable.
_LOCKED_STATUSES = {"POSTED", "REVERSED"}


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict]]:
    """Fetch accounts and tax_codes for the JE form dropdowns."""
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])
    t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])
    return accounts, tax_codes


@router.get("/journal-entries/{entry_id}/edit", response_class=HTMLResponse, response_model=None)
async def journal_entry_edit_form(
    request: Request,
    entry_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing journal entry.

    Only DRAFT entries are editable.  POSTED or REVERSED entries get the
    read-only ``edit_blocked.html`` page instead of the form.

    The current ``version`` is stored in a hidden input so the subsequent POST
    can include it in the ``If-Match`` header for optimistic locking.  A fresh
    idempotency key is generated per GET to guard against double-submit on
    page reload.

    Line fields pre-populated: account_id, description, debit, credit.
    """
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
            "journal_entries/edit.html",
            {
                "entry": None, "form": {},
                "errors": {"__all__": "Journal entry not found"},
                "conflict": False, "accounts": [], "tax_codes": [], "lines": [], "line_count": 0,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "journal_entries/edit.html",
            {
                "entry": None, "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False, "accounts": [], "tax_codes": [], "lines": [], "line_count": 0,
            },
            status_code=resp.status_code,
        )

    entry = resp.json()

    # Block editing of non-DRAFT entries.
    if entry.get("status") in _LOCKED_STATUSES:
        return _TEMPLATES.TemplateResponse(
            request,
            "journal_entries/edit_blocked.html",
            {"entry": entry},
            status_code=422,
        )

    # Pre-populate the form dict from the API response.
    # narration is stored as `description` in JournalEntryOut but submitted as `narration`.
    form: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        if field == "narration":
            # Out schema uses `description`; form field name is `narration`.
            form["narration"] = entry.get("description") or ""
        else:
            form[field] = entry.get(field) or ""
    form["version"] = str(entry.get("version", ""))

    # Build lines list for the form, keyed by zero-based index.
    api_lines = entry.get("lines", [])
    lines = []
    for i, ln in enumerate(api_lines):
        lines.append({
            "index": i,
            "account_id": str(ln.get("account_id") or ""),
            "description": ln.get("description", ""),
            "debit": str(ln.get("debit", "0")),
            "credit": str(ln.get("credit", "0")),
            "tax_code_id": str(ln.get("tax_code_id") or ""),
        })
    if not lines:
        lines = [{"index": 0}, {"index": 1}]

    async with api_client(request) as client:
        accounts, tax_codes = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/edit.html",
        {
            "entry": entry,
            "form": form,
            "errors": {},
            "conflict": False,
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
        },
    )


@router.post("/journal-entries/{entry_id}/edit", response_class=HTMLResponse, response_model=None)
async def journal_entry_update(
    request: Request,
    entry_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full lines replace.

    Outcomes:
    - 200 OK       -> 303 redirect to /journal-entries/{id}  (Post-Redirect-Get)
    - 409 Conflict -> re-fetch latest record, re-render form with conflict banner
                      and the server's current version in the hidden input.
                      The user's submitted values are preserved.
    - 422          -> re-render with per-field or balance validation errors.
                      Plain-string detail (unbalanced) goes to errors["__all__"].
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
    payload["lines"] = _parse_je_lines(form)

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
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
        request.session["flash"] = "Journal entry updated."
        return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)

    if resp.status_code == 403:
        request.session["flash"] = "You do not have permission to edit this journal entry."
        return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # show a conflict banner so the user can reconcile.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/journal_entries/{entry_id}")
            server_entry: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_entry.get("version", ""))

            accounts, tax_codes = await _fetch_dropdowns(client)

        # Preserve user's submitted form values but update the hidden version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        # Reconstruct lines for re-render from submitted values.
        raw_lines = _parse_je_lines(form)
        lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [
            {"index": 0},
            {"index": 1},
        ]

        return _TEMPLATES.TemplateResponse(
            request,
            "journal_entries/edit.html",
            {
                "entry": server_entry,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_entry": server_entry,
                "idempotency_key": idempotency_key,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "lines": lines,
                "line_count": len(lines),
            },
            status_code=409,
        )

    # 422 — parse per-field or balance validation errors.
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
                # Plain string for balance violations.
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    elif resp.status_code == 428:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "PATCH /api/v1/journal_entries/%s returned 428 — If-Match header was missing",
            entry_id,
        )
        errors["__all__"] = "Precondition required: version information was missing. Please reload and try again."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch accounts and tax_codes dropdowns for re-render.
    async with api_client(request) as client:
        accounts2, tax_codes2 = await _fetch_dropdowns(client)

    raw_lines2 = _parse_je_lines(form)
    lines2 = [{"index": i, **ln} for i, ln in enumerate(raw_lines2)] or [
        {"index": 0},
        {"index": 1},
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/edit.html",
        {
            "entry": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "idempotency_key": idempotency_key,
            "accounts": accounts2,
            "tax_codes": tax_codes2,
            "lines": lines2,
            "line_count": len(lines2),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{entry_id}/archive
# NOTE: MUST appear before the catch-all /{entry_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/journal-entries/{entry_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def journal_entry_archive(
    request: Request,
    entry_id: str,
) -> RedirectResponse:
    """Soft-archive a journal entry via DELETE /api/v1/journal_entries/{id} with If-Match.

    Only DRAFT entries may be archived; the API returns 422 for POSTED/REVERSED.
    On success redirects to /journal-entries with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/journal_entries",
        entity_id=entry_id,
        version=str(version),
        entity_label=f"Journal entry {entry_id}",
        list_url="/journal-entries",
        detail_url=f"/journal-entries/{entry_id}",
    )


# ---------------------------------------------------------------------------
# Post transition — POST /{entry_id}/post
# NOTE: MUST appear before the catch-all /{entry_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/journal-entries/{entry_id}/post",
    response_class=HTMLResponse,
    response_model=None,
)
async def journal_entry_post(
    request: Request,
    entry_id: str,
) -> RedirectResponse:
    """Transition a DRAFT journal entry to POSTED.

    POSTs to POST /api/v1/journal_entries/{id}/post with If-Match +
    X-Idempotency-Key.
    - 200 -> 303 to detail with flash "Journal entry posted."
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
            f"/api/v1/journal_entries/{entry_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Journal entry posted."
        return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)


# ---------------------------------------------------------------------------
# Reverse transition — POST /{entry_id}/reverse
# NOTE: MUST appear before the catch-all /{entry_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/journal-entries/{entry_id}/reverse",
    response_class=HTMLResponse,
    response_model=None,
)
async def journal_entry_reverse(
    request: Request,
    entry_id: str,
) -> RedirectResponse:
    """Create a reversal of a POSTED journal entry.

    POSTs to POST /api/v1/journal_entries/{id}/reverse with If-Match.
    - 201 -> 303 redirect to the NEW reversal entry's detail page.
    - 409 -> 303 back to original entry with flash "Version conflict — try again."
    - 422 -> 303 back to original entry with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/journal_entries/{entry_id}/reverse",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        reversal = resp.json()
        reversal_id = reversal.get("id", entry_id)
        request.session["flash"] = "Reversal entry created."
        return RedirectResponse(url=f"/journal-entries/{reversal_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/journal-entries/{entry_id}", status_code=303)


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
                {"entry": None, "error": "Journal entry not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "journal_entries/detail.html",
                {"entry": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    entry = resp.json()
    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "journal_entries/detail.html",
        {"entry": entry, "error": None, "flash": flash},
    )
