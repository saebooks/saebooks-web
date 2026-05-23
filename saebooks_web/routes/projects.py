"""Projects list, detail, create, edit, archive — Lane D cycles 22 + 34.

GET  /projects           — list page (paginated, HTMX-aware)
GET  /projects/new       — empty create form; generates idempotency key
POST /projects/new       — submit to upstream API; redirect on success,
                           re-render with errors on 422
GET  /projects/{id}      — project detail
GET  /projects/{id}/edit — pre-populated edit form (version in hidden input)
                           If project is archived -> 422 + edit_blocked.html
POST /projects/{id}/edit — submit PATCH to API with If-Match; redirect on
                           success, re-render on 409 (conflict) or 422
POST /projects/{id}/archive — soft-archive via archive_entity helper

Route ordering: /new + /{id}/edit + /{id}/archive MUST appear before the
catch-all /{id} GET so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.

Projects are flat job/cost-centre entities (tier-4) used for
job costing and project-level P&L reporting.

The API uses page/page_size pagination (same as bills/invoices).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.archive_helpers import archive_entity as _archive_entity

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Status choices — used in form templates.
PROJECT_STATUSES = [
    ("ACTIVE", "Active"),
    ("ON_HOLD", "On Hold"),
    ("COMPLETED", "Completed"),
    ("ARCHIVED", "Archived"),
]

# Fields present on both create and edit forms.
_EDIT_FIELDS = (
    "code",
    "name",
    "status",
    "start_date",
    "end_date",
    "notes",
)


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{project_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/projects/new", response_class=HTMLResponse, response_model=None)
async def project_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-project form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "projects/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "project_statuses": PROJECT_STATUSES,
        },
    )


@router.post("/projects/new", response_class=HTMLResponse, response_model=None)
async def project_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-project form.

    - 201 -> 303 redirect to /projects/{id}
    - 422 -> re-render form with per-field errors (or __all__ for string errors)
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Required fields always included.
    payload: dict[str, object] = {}
    for required_field in ("code", "name"):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    # Optional fields — include only when non-empty.
    for optional_field in ("status", "start_date", "end_date", "notes"):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/projects",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/projects/{created['id']}", status_code=303)

    # 422 — parse per-field or plain-string errors.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field = str(field_parts[0]) if field_parts else "__all__"
                    errors[field] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "projects/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "project_statuses": PROJECT_STATUSES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before /{project_id} catch-all.
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/edit", response_class=HTMLResponse, response_model=None)
async def project_edit_form(
    request: Request,
    project_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing project.

    If the project is already archived, renders edit_blocked.html with HTTP 422.
    """
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
            "projects/edit.html",
            {
                "project": None,
                "form": {},
                "errors": {"__all__": "Project not found"},
                "conflict": False,
                "project_statuses": PROJECT_STATUSES,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "projects/edit.html",
            {
                "project": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "project_statuses": PROJECT_STATUSES,
            },
            status_code=resp.status_code,
        )

    project = resp.json()

    # Block editing archived projects.
    if project.get("archived_at"):
        return _TEMPLATES.TemplateResponse(
            request,
            "projects/edit_blocked.html",
            {"project": project},
            status_code=422,
        )

    form: dict[str, str] = {field: str(project.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(project.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "projects/edit.html",
        {
            "project": project,
            "form": form,
            "errors": {},
            "conflict": False,
            "project_statuses": PROJECT_STATUSES,
        },
    )


@router.post("/projects/{project_id}/edit", response_class=HTMLResponse, response_model=None)
async def project_update(
    request: Request,
    project_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /projects/{id}
    - 409 Conflict -> re-fetch latest, re-render with conflict banner + server version
    - 422          -> re-render with per-field validation errors
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    # Build PATCH payload — only include non-empty fields.
    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/projects/{project_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Project saved."
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/projects/{project_id}")

        server_project: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_project.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "projects/edit.html",
            {
                "project": server_project,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_project": server_project,
                "project_statuses": PROJECT_STATUSES,
            },
            status_code=409,
        )

    # 422 — per-field validation errors.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field = str(field_parts[0]) if field_parts else "__all__"
                    errors[field] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    elif resp.status_code == 428:
        import logging as _logging

        _logging.getLogger(__name__).error(
            "PATCH /api/v1/projects/%s returned 428 — If-Match header was missing",
            project_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "projects/edit.html",
        {
            "project": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "project_statuses": PROJECT_STATUSES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{project_id}/archive
# NOTE: MUST appear before the catch-all /{project_id} GET.
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/archive", response_class=HTMLResponse, response_model=None)
async def project_archive(
    request: Request,
    project_id: str,
) -> RedirectResponse:
    """Soft-archive a project via DELETE /api/v1/projects/{id} with If-Match.

    On success redirects to /projects with a flash.
    On 409 (version conflict) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/projects",
        entity_id=project_id,
        version=str(version),
        entity_label=f"Project {project_id}",
        list_url="/projects",
        detail_url=f"/projects/{project_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Bulk action — POST /projects/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_PROJECTS = {
    "archive": ("DELETE", "/api/v1/projects/{id}"),
}


@router.post("/projects/bulk", response_class=HTMLResponse, response_model=None)
async def projects_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many projects at once.

    Form fields:
      action  — one of: archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /projects. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_PROJECTS:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/projects", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/projects", status_code=303)

    method, path_tpl = _BULK_ACTIONS_PROJECTS[action]
    ok = 0
    failed: list[str] = []
    async with api_client(request) as client:
        for row_id in ids:
            try:
                resp = await client.request(method, path_tpl.format(id=row_id))
                if 200 <= resp.status_code < 300:
                    ok += 1
                else:
                    msg = ""
                    try:
                        body = resp.json()
                        detail = body.get("detail")
                        if isinstance(detail, str):
                            msg = detail
                        elif isinstance(detail, list) and detail:
                            msg = detail[0].get("msg", str(detail))
                    except Exception:
                        msg = ""
                    failed.append(f"{row_id[:8]} ({resp.status_code}{': ' + msg if msg else ''})")
            except Exception as exc:
                failed.append(f"{row_id[:8]} (transport error: {exc!s})")

    label = action.replace("_", " ").title()
    if failed:
        request.session["flash"] = (
            f"{label}: {ok} succeeded, {len(failed)} failed — " + "; ".join(failed[:5])
            + (f" … +{len(failed) - 5} more" if len(failed) > 5 else "")
        )
    else:
        request.session["flash"] = f"{label}: {ok} project{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/projects", status_code=303)
