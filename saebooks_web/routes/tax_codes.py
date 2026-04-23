"""Tax codes list + detail views — Lane D cycle 9.

GET /tax-codes
    Renders templates/tax_codes/list.html (full page) or
    templates/tax_codes/_table.html (HTMX fragment when HX-Request header present).
    Query params: limit (default 200), offset.
    Calls GET /api/v1/tax_codes with matching params.

GET /tax-codes/{id}
    Renders templates/tax_codes/detail.html.
    Calls GET /api/v1/tax_codes/{id}.

Auth guard: redirect to /login (303) if no session token.

Note: upstream router prefix is /tax_codes (underscore) but the web route
uses /tax-codes (hyphen) for a cleaner URL.
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


@router.get("/tax-codes", response_class=HTMLResponse, response_model=None)
async def tax_codes_list(
    request: Request,
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the tax codes list page (full or HTMX fragment)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}

    error: str | None = None
    tax_codes: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/tax_codes", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            tax_codes = payload.get("items", [])
            total = payload.get("total", len(tax_codes))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset: int | None = offset - limit if offset > 0 else None
    next_offset: int | None = offset + limit if offset + limit < total else None

    ctx = {
        "tax_codes": tax_codes,
        "total": total,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "error": error,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "tax_codes/_table.html" if is_htmx else "tax_codes/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/tax-codes/{tax_code_id}", response_class=HTMLResponse, response_model=None)
async def tax_code_detail(
    request: Request,
    tax_code_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single tax code detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/tax_codes/{tax_code_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "tax_codes/detail.html",
                {"tax_code": None, "error": "Tax code not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "tax_codes/detail.html",
                {"tax_code": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    tax_code = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "tax_codes/detail.html",
        {"tax_code": tax_code, "error": None},
    )
