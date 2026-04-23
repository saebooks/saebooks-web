"""Recurring invoices list and detail views — Lane D cycle 26.

GET  /recurring-invoices       — list page (paginated, HTMX-aware)
GET  /recurring-invoices/{id}  — recurring invoice detail

Auth guard: redirect to /login (303) if no session token.

RecurrenceStatus values: ACTIVE / PAUSED / ENDED.
RecurrenceFrequency values: WEEKLY / FORTNIGHTLY / MONTHLY / QUARTERLY / YEARLY.

The API prefix is /api/v1/recurring_invoices and uses page/page_size pagination.
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
        {"ri": ri, "error": None, "flash": flash},
    )
