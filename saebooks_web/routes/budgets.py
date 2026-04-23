"""Budgets list and detail views — Lane D cycle 27.

GET  /budgets       — list page (paginated, HTMX-aware)
GET  /budgets/{id}  — budget detail

Auth guard: redirect to /login (303) if no session token.

Budgets are tier-4 read-only flat records (account × year × month → amount).
No create/edit form in this cycle.

The API uses page/page_size pagination and the prefix is /api/v1/budgets.
Filters: year (int), month (1-12).
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


@router.get("/budgets", response_class=HTMLResponse, response_model=None)
async def budgets_list(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    page_size: int = 50,
    page: int = 1,
) -> HTMLResponse | RedirectResponse:
    """Render the budgets list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``budgets/_table.html`` partial only.  Otherwise the full page
    (``budgets/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if year is not None:
        params["year"] = year
    if month is not None:
        params["month"] = month

    error: str | None = None
    budgets: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/budgets", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            budgets = payload.get("items", [])
            total = payload.get("total", len(budgets))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Compute offset equivalents for pagination links.
    offset = (page - 1) * page_size
    prev_page = page - 1 if page > 1 else None
    next_page = page + 1 if (offset + page_size) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "budgets": budgets,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_year": year,
        "filter_month": month,
        "page": page,
        "page_size": page_size,
        "prev_page": prev_page,
        "next_page": next_page,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "budgets/_table.html" if is_htmx else "budgets/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/budgets/{budget_id}", response_class=HTMLResponse, response_model=None)
async def budget_detail(
    request: Request,
    budget_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single budget detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/budgets/{budget_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "budgets/detail.html",
                {"budget": None, "error": "Budget not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "budgets/detail.html",
                {"budget": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    budget = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "budgets/detail.html",
        {"budget": budget, "error": None, "flash": flash},
    )
