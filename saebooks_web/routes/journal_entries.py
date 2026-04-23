"""Journal entries list + detail views — Lane D cycle 6.

GET /journal-entries
    Renders templates/journal_entries/list.html (full page) or
    templates/journal_entries/_table.html (HTMX fragment when HX-Request header present).
    Query params: status, date_from, date_to, limit (default 50), offset.
    Calls GET /api/v1/journal_entries with matching params.

GET /journal-entries/{id}
    Renders templates/journal_entries/detail.html.
    Calls GET /api/v1/journal_entries/{id}.

API shape (from JournalEntryOut / JournalLineOut):
- ref: str — journal reference like JE-000001
- entry_date: date — primary display date
- description: str | None — one-line summary / narration
- status: str — DRAFT / POSTED / REVERSED
- posted_at: datetime | None
- lines[]: line_no, account_id, description, debit (Decimal), credit (Decimal)
- No top-level total_debit / total_credit — computed in template from lines.

Divergences from credit_notes pattern:
- URL slug is /journal-entries (hyphenated); API path uses /api/v1/journal_entries.
- No contact_id / source filter — filters are status + date range only.
- Lines use debit + credit columns (standard ledger), not quantity/unit_price.
- Status values: DRAFT / POSTED / REVERSED (not VOIDED).

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


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
