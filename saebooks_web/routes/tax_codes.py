"""Tax codes list, detail, create, edit, archive — Lane D cycles 9 + 24.

GET  /tax-codes              — list page (HTMX-aware, limit/offset pagination)
GET  /tax-codes/new          — empty create form; generates idempotency key
POST /tax-codes/new          — submit to upstream API; redirect on success,
                              re-render with errors on 422
GET  /tax-codes/{id}         — tax code detail (flash from session)
GET  /tax-codes/{id}/edit    — pre-populated edit form (version in hidden input)
                              If tax code is archived -> 422 + edit_blocked.html
POST /tax-codes/{id}/edit    — submit PATCH to API with If-Match; redirect on
                              success, re-render on 409 (conflict) or 422
POST /tax-codes/{id}/archive — soft-archive via archive_entity helper

Route ordering: /new + /{id}/edit + /{id}/archive MUST appear before the
catch-all /{id} GET so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.

Note: upstream router prefix is /tax_codes (underscore) but the web route
uses /tax-codes (hyphen) for a cleaner URL.
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

# Tax system options for the select dropdown.
TAX_SYSTEMS = [
    ("GST", "GST"),
    ("VAT", "VAT"),
    ("other", "Other"),
]

# Reporting type options for the select dropdown (BAS buckets).
REPORTING_TYPES = [
    ("taxable", "Taxable"),
    ("gst_free", "GST Free / Zero-rated"),
    ("input_taxed", "Input Taxed"),
    ("out_of_scope", "Out of scope"),
    ("exempt", "Exempt"),
]


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


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

    # Consume and clear any flash message (e.g. from a successful archive/edit).
    flash = request.session.pop("flash", None)

    ctx = {
        "tax_codes": tax_codes,
        "total": total,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "error": error,
        "flash": flash,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "tax_codes/_table.html" if is_htmx else "tax_codes/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{tax_code_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/tax-codes/new", response_class=HTMLResponse, response_model=None)
async def tax_code_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-tax-code form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "tax_codes/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "tax_systems": TAX_SYSTEMS,
            "reporting_types": REPORTING_TYPES,
        },
    )


@router.post("/tax-codes/new", response_class=HTMLResponse, response_model=None)
async def tax_code_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-tax-code form.

    - 201 -> 303 redirect to /tax-codes/{id}
    - 422 -> re-render form with per-field errors (or __all__ for string errors)
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the payload. Required fields always included; optional only when non-empty.
    payload: dict[str, object] = {}

    for required_field in ("code", "name", "rate", "tax_system", "reporting_type"):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    for optional_field in ("description",):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/tax_codes",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/tax-codes/{created['id']}", status_code=303)

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
        "tax_codes/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "tax_systems": TAX_SYSTEMS,
            "reporting_types": REPORTING_TYPES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before /{tax_code_id} catch-all.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "code",
    "name",
    "rate",
    "tax_system",
    "reporting_type",
    "description",
)


@router.get("/tax-codes/{tax_code_id}/edit", response_class=HTMLResponse, response_model=None)
async def tax_code_edit_form(
    request: Request,
    tax_code_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing tax code.

    If the tax code is already archived, renders edit_blocked.html with HTTP 422.
    """
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
            "tax_codes/edit.html",
            {
                "tax_code": None,
                "form": {},
                "errors": {"__all__": "Tax code not found"},
                "conflict": False,
                "tax_systems": TAX_SYSTEMS,
                "reporting_types": REPORTING_TYPES,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "tax_codes/edit.html",
            {
                "tax_code": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "tax_systems": TAX_SYSTEMS,
                "reporting_types": REPORTING_TYPES,
            },
            status_code=resp.status_code,
        )

    tax_code = resp.json()

    # Block editing archived tax codes.
    if tax_code.get("archived_at"):
        return _TEMPLATES.TemplateResponse(
            request,
            "tax_codes/edit_blocked.html",
            {"tax_code": tax_code},
            status_code=422,
        )

    form: dict[str, str] = {field: str(tax_code.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(tax_code.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "tax_codes/edit.html",
        {
            "tax_code": tax_code,
            "form": form,
            "errors": {},
            "conflict": False,
            "tax_systems": TAX_SYSTEMS,
            "reporting_types": REPORTING_TYPES,
        },
    )


@router.post("/tax-codes/{tax_code_id}/edit", response_class=HTMLResponse, response_model=None)
async def tax_code_update(
    request: Request,
    tax_code_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /tax-codes/{id}
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
            f"/api/v1/tax_codes/{tax_code_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Tax code saved."
        return RedirectResponse(url=f"/tax-codes/{tax_code_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/tax_codes/{tax_code_id}")

        server_tax_code: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_tax_code.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "tax_codes/edit.html",
            {
                "tax_code": server_tax_code,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_tax_code": server_tax_code,
                "tax_systems": TAX_SYSTEMS,
                "reporting_types": REPORTING_TYPES,
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
            "PATCH /api/v1/tax_codes/%s returned 428 — If-Match header was missing",
            tax_code_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "tax_codes/edit.html",
        {
            "tax_code": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "tax_systems": TAX_SYSTEMS,
            "reporting_types": REPORTING_TYPES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{tax_code_id}/archive
# NOTE: MUST appear before the catch-all /{tax_code_id} GET.
# ---------------------------------------------------------------------------


@router.post("/tax-codes/{tax_code_id}/archive", response_class=HTMLResponse, response_model=None)
async def tax_code_archive(
    request: Request,
    tax_code_id: str,
) -> RedirectResponse:
    """Soft-archive a tax code via DELETE /api/v1/tax_codes/{id} with If-Match.

    On success redirects to /tax-codes with a flash.
    On 409 (version conflict) or 422 (tax code in use) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/tax_codes",
        entity_id=tax_code_id,
        version=str(version),
        entity_label=f"Tax code {tax_code_id}",
        list_url="/tax-codes",
        detail_url=f"/tax-codes/{tax_code_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


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
                {"tax_code": None, "error": "Tax code not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "tax_codes/detail.html",
                {"tax_code": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    tax_code = resp.json()
    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "tax_codes/detail.html",
        {"tax_code": tax_code, "error": None, "flash": flash},
    )
