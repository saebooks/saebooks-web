"""Fixed assets list and detail views — Lane D cycle 26.

GET  /fixed-assets       — list page (paginated, HTMX-aware)
GET  /fixed-assets/{id}  — fixed asset detail

Auth guard: redirect to /login (303) if no session token.

Fixed assets are tier-4 read-only views.  No create/edit form in this cycle.

The API uses page/page_size pagination and the prefix is /api/v1/fixed_assets.
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


@router.get("/fixed-assets", response_class=HTMLResponse, response_model=None)
async def fixed_assets_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the fixed assets list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``fixed_assets/_table.html`` partial only.  Otherwise the full page
    (``fixed_assets/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # The API uses page/page_size rather than limit/offset.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status

    error: str | None = None
    assets: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/fixed_assets", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            assets = payload.get("items", [])
            total = payload.get("total", len(assets))
            # Client-side search filter (name/code) — the API has no free-text
            # search param so we filter locally on the returned page.
            if search:
                q = search.lower()
                assets = [
                    a for a in assets
                    if q in (a.get("name") or "").lower()
                    or q in (a.get("code") or "").lower()
                ]
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "assets": assets,
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
    template = "fixed_assets/_table.html" if is_htmx else "fixed_assets/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/fixed-assets/{asset_id}", response_class=HTMLResponse, response_model=None)
async def fixed_asset_detail(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single fixed asset detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/fixed_assets/{asset_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "fixed_assets/detail.html",
                {"asset": None, "error": "Fixed asset not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "fixed_assets/detail.html",
                {"asset": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    asset = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/detail.html",
        {"asset": asset, "error": None, "flash": flash},
    )
