"""Supplier statement reconciliation views — Gitea #28, Phase 1-3.

Route map
---------
GET  /statements                          — recon queue (default: actionable only)
GET  /statements/{id}                     — recon detail: header card + lines table
POST /statements/ingest                   — ingest a Paperless doc → redirect to detail
POST /statements/{id}/draft-missing-bill  — draft a bill from a missing line
POST /statements/{id}/dismiss             — dismiss the statement
POST /statements/{id}/confirm             — confirm / mark reviewed

Route ordering: /statements/ingest MUST be declared before /statements/{id}
so FastAPI resolves the literal path first.

API endpoints consumed:
- GET  /api/v1/statements?status=&limit=&offset=
- GET  /api/v1/statements/{id}
- POST /api/v1/statements/ingest                 body: {"paperless_document_id": int}
- POST /api/v1/statements/{id}/draft-missing-bill body: {"line_id": "<uuid>"}
- POST /api/v1/statements/{id}/dismiss
- POST /api/v1/statements/{id}/confirm

Auth guard: redirect to /login (303) if no session token.

Queue default-filter logic (Part A)
------------------------------------
When no ?status= param is given the queue defaults to "Needs attention" — i.e. it
fetches ALL statements from the API (no status param) and then client-side-filters to
only show actionable ones (needs_review, extracted).  This keeps the default view
free of reconciled/dismissed clutter while requiring only one API call.  The tab
links expose the four views: Needs attention (default), Reconciled, Dismissed, All.
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

# Statuses that need the user's attention (shown in the default "Needs attention" view)
_ACTIONABLE_STATUSES: frozenset[str] = frozenset({"needs_review", "extracted"})


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# Match-status sort order: exceptions first, then matched, then informational.
# Lower rank = listed earlier.
_MATCH_STATUS_RANK: dict[str, int] = {
    "missing_in_books":     0,
    "amount_mismatch":      1,
    "not_on_statement":     2,
    "matched":              3,
    "settled_not_in_books": 4,
    "payment_info":         5,
}


def _sort_lines(lines: list[dict]) -> list[dict]:
    """Sort statement lines: exceptions first, then matched, then informational."""
    return sorted(
        lines,
        key=lambda ln: _MATCH_STATUS_RANK.get(
            (ln.get("match_status") or "").lower(), 99
        ),
    )


# ---------------------------------------------------------------------------
# GET /statements — recon queue
# ---------------------------------------------------------------------------


@router.get("/statements", response_class=HTMLResponse, response_model=None)
async def statements_list(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the supplier-statement reconciliation queue.

    Default view (no ?status=) shows only actionable statements (needs_review,
    extracted).  Explicit ?status= values pass through to the API unchanged,
    except ?status=all which fetches without a status filter.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    statements: list[dict] = []
    total: int = 0

    # Determine whether we are in "needs attention" (default) mode or an
    # explicit single-status / "all" mode.
    is_default_view = status is None  # "Needs attention"
    effective_status = status  # passed to API (None = fetch all)
    if effective_status == "all":
        effective_status = None  # "All" tab: no server-side filter

    params: dict[str, str | int] = {"limit": limit, "offset": offset}
    if effective_status:
        params["status"] = effective_status

    async with api_client(request) as client:
        resp = await client.get("/api/v1/statements", params=params)

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            all_items: list[dict] = payload.get("items", [])
            total_from_api: int = payload.get("total", len(all_items))

            if is_default_view:
                # Client-side filter: only actionable statuses
                statements = [
                    s for s in all_items
                    if (s.get("status") or "").lower() in _ACTIONABLE_STATUSES
                ]
                total = len(statements)
            else:
                statements = all_items
                total = total_from_api
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "statements/index.html",
        {
            "statements": statements,
            "total": total,
            "status_filter": status or "",  # "" = default "Needs attention" tab
            "limit": limit,
            "offset": offset,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /statements/ingest — ingest a Paperless document
# NOTE: MUST appear before /statements/{id}
# ---------------------------------------------------------------------------


@router.post("/statements/ingest", response_class=HTMLResponse, response_model=None)
async def statements_ingest(request: Request) -> RedirectResponse:
    """Ingest a Paperless document by ID → redirect to the new statement detail.

    On error, redirects back to /statements with a flash message.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    raw = str(form_data.get("paperless_document_id", "")).strip()

    if not raw.isdigit():
        request.session["flash"] = "Invalid document ID — must be a number."
        return RedirectResponse(url="/statements", status_code=303)

    doc_id = int(raw)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/statements/ingest",
            json={"paperless_document_id": doc_id},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (200, 201):
        try:
            detail = resp.json()
            stmt_id = detail.get("id")
            if stmt_id:
                return RedirectResponse(url=f"/statements/{stmt_id}", status_code=303)
        except Exception:  # noqa: BLE001
            pass
        request.session["flash"] = "Statement ingested."
        return RedirectResponse(url="/statements", status_code=303)

    # Error path — put detail in flash and return to queue
    try:
        msg = resp.json().get("detail", f"Ingest failed: HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001
        msg = f"Ingest failed: HTTP {resp.status_code}"
    request.session["flash"] = str(msg)
    return RedirectResponse(url="/statements", status_code=303)


# ---------------------------------------------------------------------------
# POST /statements/{id}/draft-missing-bill
# NOTE: MUST appear before /statements/{id}
# ---------------------------------------------------------------------------


@router.post(
    "/statements/{statement_id}/draft-missing-bill",
    response_class=HTMLResponse,
    response_model=None,
)
async def statements_draft_missing_bill(
    request: Request,
    statement_id: str,
) -> RedirectResponse:
    """Draft a bill from a missing-in-books line.

    Reads ``line_id`` from the form.  On success (201) the API returns
    ``{"bill_id": "...", "statement": {...}}`` — redirect to /bills/{bill_id}
    so the user can code the new draft immediately.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    line_id = str(form_data.get("line_id", "")).strip()
    if not line_id:
        request.session["flash"] = "Missing line_id — cannot draft bill."
        return RedirectResponse(url=f"/statements/{statement_id}", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/statements/{statement_id}/draft-missing-bill",
            json={"line_id": line_id},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        try:
            payload = resp.json()
            bill_id = payload.get("bill_id")
            if bill_id:
                request.session["flash"] = "Draft bill created — code it below."
                return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)
        except Exception:  # noqa: BLE001
            pass
        request.session["flash"] = "Draft bill created."
        return RedirectResponse(url=f"/statements/{statement_id}", status_code=303)

    # Error path
    try:
        msg = resp.json().get("detail", f"Draft bill failed: HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001
        msg = f"Draft bill failed: HTTP {resp.status_code}"
    request.session["flash"] = str(msg)
    return RedirectResponse(url=f"/statements/{statement_id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /statements/{id}/dismiss
# NOTE: MUST appear before /statements/{id}
# ---------------------------------------------------------------------------


@router.post(
    "/statements/{statement_id}/dismiss",
    response_class=HTMLResponse,
    response_model=None,
)
async def statements_dismiss(
    request: Request,
    statement_id: str,
) -> RedirectResponse:
    """Dismiss a statement → redirect to the queue with a flash."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/statements/{statement_id}/dismiss",
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = "Statement dismissed."
        return RedirectResponse(url="/statements", status_code=303)

    try:
        msg = resp.json().get("detail", f"Dismiss failed: HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001
        msg = f"Dismiss failed: HTTP {resp.status_code}"
    request.session["flash"] = str(msg)
    return RedirectResponse(url=f"/statements/{statement_id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /statements/{id}/confirm
# NOTE: MUST appear before /statements/{id}
# ---------------------------------------------------------------------------


@router.post(
    "/statements/{statement_id}/confirm",
    response_class=HTMLResponse,
    response_model=None,
)
async def statements_confirm(
    request: Request,
    statement_id: str,
) -> RedirectResponse:
    """Confirm / mark reviewed → redirect back to the detail with a flash."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/statements/{statement_id}/confirm",
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = "Statement marked as reviewed."
        return RedirectResponse(url=f"/statements/{statement_id}", status_code=303)

    try:
        msg = resp.json().get("detail", f"Confirm failed: HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001
        msg = f"Confirm failed: HTTP {resp.status_code}"
    request.session["flash"] = str(msg)
    return RedirectResponse(url=f"/statements/{statement_id}", status_code=303)


# ---------------------------------------------------------------------------
# GET /statements/{id} — recon detail
# NOTE: MUST appear after all /statements/{id}/… routes
# ---------------------------------------------------------------------------


@router.get("/statements/{statement_id}", response_class=HTMLResponse, response_model=None)
async def statements_detail(
    request: Request,
    statement_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the reconciliation detail for a single supplier statement."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    statement: dict | None = None
    lines: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/statements/{statement_id}")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.status_code == 404:
            return HTMLResponse(
                _TEMPLATES.get_template("statements/detail.html").render(
                    {
                        "request": request,
                        "statement": None,
                        "lines": [],
                        "error": "Statement not found.",
                        "flash": None,
                    }
                ),
                status_code=404,
            )

        if resp.is_success:
            statement = resp.json()
            lines = _sort_lines(statement.pop("lines", []))
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "statements/detail.html",
        {
            "statement": statement,
            "lines": lines,
            "error": error,
            "flash": flash,
        },
    )
