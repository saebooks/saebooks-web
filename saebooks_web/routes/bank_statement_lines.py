"""Bank statement lines list and detail views — Lane D cycle 27.

GET  /bank-statement-lines       — list page (paginated, HTMX-aware)
GET  /bank-statement-lines/{id}  — bank statement line detail

Auth guard: redirect to /login (303) if no session token.

Bank statement lines are tier-4 read-only views.  No create/edit form in this cycle.

The API uses limit/offset pagination and the prefix is /api/v1/bank_statement_lines.
Filters: bank_account_id (UUID), status (UNMATCHED/MATCHED/IGNORED/RECONCILED).
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


@router.get("/bank-statement-lines", response_class=HTMLResponse, response_model=None)
async def bank_statement_lines_list(
    request: Request,
    status: str | None = None,
    bank_account_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the bank statement lines list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``bank_statement_lines/_table.html`` partial only.  Otherwise the full
    page (``bank_statement_lines/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if bank_account_id:
        params["bank_account_id"] = bank_account_id

    error: str | None = None
    lines: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank_statement_lines", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            lines = payload.get("items", [])
            total = payload.get("total", len(lines))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "lines": lines,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_status": status or "",
        "filter_bank_account_id": bank_account_id or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = (
        "bank_statement_lines/_table.html" if is_htmx
        else "bank_statement_lines/list.html"
    )

    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/bank-statement-lines/{line_id}", response_class=HTMLResponse, response_model=None)
async def bank_statement_line_detail(
    request: Request,
    line_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single bank statement line detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_statement_lines/{line_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_statement_lines/detail.html",
                {"line": None, "error": "Bank statement line not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_statement_lines/detail.html",
                {"line": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    line = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "bank_statement_lines/detail.html",
        {"line": line, "error": None, "flash": flash},
    )
