"""Bills list + detail views — Lane D cycle 3.

GET /bills
    Renders templates/bills/list.html (full page) or
    templates/bills/_table.html (HTMX fragment when HX-Request header present).
    Query params: status, contact_id, date_from, date_to, limit (default 50), offset.
    Calls GET /api/v1/bills with matching params.

GET /bills/{id}
    Renders templates/bills/detail.html.
    Calls GET /api/v1/bills/{id}.

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
                {"bill": None, "error": "Bill not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bills/detail.html",
                {"bill": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    bill = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "bills/detail.html",
        {"bill": bill, "error": None},
    )
