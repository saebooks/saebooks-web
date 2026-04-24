"""Budgets list, detail, create and edit views — Lane D cycles 27 + 31.

GET  /budgets/new        — empty create form
POST /budgets/new        — submit to API; 303 on success, 422 re-render on error
GET  /budgets/{id}/edit  — pre-populated edit form (version in hidden input)
                          If archived_at → edit_blocked.html (HTTP 422)
POST /budgets/{id}/edit  — PATCH with If-Match; 303 with flash on success
POST /budgets/{id}/archive — soft-archive via archive_entity helper
GET  /budgets            — list page (paginated, HTMX-aware)
GET  /budgets/{id}       — budget detail

Route ordering: /new + /{id}/edit + /{id}/archive MUST appear before /{id}
so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.
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


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


async def _fetch_accounts(request: Request) -> list[dict]:
    """Fetch all accounts for dropdown population (max 500)."""
    async with api_client(request) as client:
        resp = await client.get("/api/v1/accounts", params={"limit": 500, "offset": 0})
    if resp.is_success:
        return resp.json().get("items", [])
    return []


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{budget_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/budgets/new", response_class=HTMLResponse, response_model=None)
async def budget_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-budget form with account dropdown."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts = await _fetch_accounts(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "budgets/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
        },
    )


@router.post("/budgets/new", response_class=HTMLResponse, response_model=None)
async def budget_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-budget form.

    - 201 -> 303 redirect to /budgets/{id}
    - 422 -> re-render form with per-field errors (or __all__ for duplicate)
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build payload — required fields always included; optional only when non-empty.
    payload: dict[str, object] = {}

    for required_field in ("account_id", "year", "month", "amount"):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    notes = form.get("notes", "").strip()
    if notes:
        payload["notes"] = notes

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/budgets",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/budgets/{created['id']}", status_code=303)

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

    accounts = await _fetch_accounts(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "budgets/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "accounts": accounts,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before /{budget_id} catch-all.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = ("account_id", "year", "month", "amount", "notes")


@router.get("/budgets/{budget_id}/edit", response_class=HTMLResponse, response_model=None)
async def budget_edit_form(
    request: Request,
    budget_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing budget.

    If the budget is already archived, renders edit_blocked.html with HTTP 422.
    """
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
            "budgets/edit.html",
            {
                "budget": None,
                "form": {},
                "errors": {"__all__": "Budget not found"},
                "conflict": False,
                "accounts": [],
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "budgets/edit.html",
            {
                "budget": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "accounts": [],
            },
            status_code=resp.status_code,
        )

    budget = resp.json()

    # Block editing archived budgets.
    if budget.get("archived_at"):
        return _TEMPLATES.TemplateResponse(
            request,
            "budgets/edit_blocked.html",
            {"budget": budget},
            status_code=422,
        )

    accounts = await _fetch_accounts(request)

    form: dict[str, str] = {field: str(budget.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(budget.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "budgets/edit.html",
        {
            "budget": budget,
            "form": form,
            "errors": {},
            "conflict": False,
            "accounts": accounts,
        },
    )


@router.post("/budgets/{budget_id}/edit", response_class=HTMLResponse, response_model=None)
async def budget_update(
    request: Request,
    budget_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK      -> 303 redirect to /budgets/{id}
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
            f"/api/v1/budgets/{budget_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Budget saved."
        return RedirectResponse(url=f"/budgets/{budget_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/budgets/{budget_id}")

        server_budget: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_budget.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        accounts = await _fetch_accounts(request)

        return _TEMPLATES.TemplateResponse(
            request,
            "budgets/edit.html",
            {
                "budget": server_budget,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_budget": server_budget,
                "accounts": accounts,
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
            "PATCH /api/v1/budgets/%s returned 428 — If-Match header was missing",
            budget_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts = await _fetch_accounts(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "budgets/edit.html",
        {
            "budget": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "accounts": accounts,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{budget_id}/archive
# NOTE: MUST appear before the catch-all /{budget_id} GET.
# ---------------------------------------------------------------------------


@router.post("/budgets/{budget_id}/archive", response_class=HTMLResponse, response_model=None)
async def budget_archive(
    request: Request,
    budget_id: str,
) -> RedirectResponse:
    """Soft-archive a budget via DELETE /api/v1/budgets/{id} with If-Match.

    On success redirects to /budgets with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/budgets",
        entity_id=budget_id,
        version=str(version),
        entity_label=f"Budget {budget_id}",
        list_url="/budgets",
        detail_url=f"/budgets/{budget_id}",
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


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
