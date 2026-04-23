"""Projects list and detail views — Lane D cycle 22.

GET  /projects       — list page (paginated, HTMX-aware)
GET  /projects/{id} — project detail

Auth guard: redirect to /login (303) if no session token.

Projects are flat job/cost-centre entities (tier-4) used for
job costing and project-level P&L reporting.  Read-only views only —
no create/edit form yet.

The API uses page/page_size pagination (same as bills/invoices).
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


@router.get("/projects", response_class=HTMLResponse, response_model=None)
async def projects_list(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the projects list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``projects/_table.html`` partial only.  Otherwise the full page
    (``projects/list.html``) is returned.
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
    projects: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/projects", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            projects = payload.get("items", [])
            total = payload.get("total", len(projects))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    # Consume and clear any flash message.
    flash = request.session.pop("flash", None)

    ctx = {
        "projects": projects,
        "total": total,
        "error": error,
        "flash": flash,
        # Filter values echoed back to the form.
        "filter_status": status or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    # HTMX requests get just the table fragment.
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "projects/_table.html" if is_htmx else "projects/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/projects/{project_id}", response_class=HTMLResponse, response_model=None)
async def project_detail(
    request: Request,
    project_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single project detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/projects/{project_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "projects/detail.html",
                {"project": None, "error": "Project not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "projects/detail.html",
                {"project": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    project = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "projects/detail.html",
        {"project": project, "error": None, "flash": flash},
    )
