"""Items list + detail views — Lane D cycle 9.

GET /items
    Renders templates/items/list.html (full page) or
    templates/items/_table.html (HTMX fragment when HX-Request header present).
    Query params: item_type, limit (default 200), offset.
    Calls GET /api/v1/items with matching params.

GET /items/{id}
    Renders templates/items/detail.html.
    Calls GET /api/v1/items/{id}.

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


@router.get("/items", response_class=HTMLResponse, response_model=None)
async def items_list(
    request: Request,
    item_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the items list page (full or HTMX fragment)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if item_type:
        params["item_type"] = item_type

    error: str | None = None
    items: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/items", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            items = payload.get("items", [])
            total = payload.get("total", len(items))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset: int | None = offset - limit if offset > 0 else None
    next_offset: int | None = offset + limit if offset + limit < total else None

    ctx = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filter_item_type": item_type or "",
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "error": error,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "items/_table.html" if is_htmx else "items/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/items/{item_id}", response_class=HTMLResponse, response_model=None)
async def item_detail(
    request: Request,
    item_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single item detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/items/{item_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "items/detail.html",
                {"item": None, "error": "Item not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "items/detail.html",
                {"item": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    item = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "items/detail.html",
        {"item": item, "error": None},
    )
