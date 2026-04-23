"""Contacts list, detail, create and edit views — Lane D cycles 1, 7 + 8.

GET  /contacts              — list page (paginated, first 100)
GET  /contacts/new          — empty create form; generates idempotency key
POST /contacts/new          — submit to upstream API; redirect on success,
                              re-render with errors on 422
GET  /contacts/{id}         — contact detail
GET  /contacts/{id}/edit    — pre-populated edit form (version in hidden input)
POST /contacts/{id}/edit    — submit PATCH to API with If-Match; redirect on
                              success, re-render on 409 (conflict) or 422

Route ordering: /contacts/new MUST be declared before /contacts/{contact_id}
so FastAPI matches the literal path first.

HTMX extension points (TODO — future cycles):
- Pagination via hx-get with ?offset= query param, swapping the table body
- Inline search with hx-trigger="keyup changed delay:300ms"
"""
from __future__ import annotations

import uuid
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


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/contacts", response_class=HTMLResponse, response_model=None)
async def contacts_list(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the contacts list page.

    Fetches the first 100 contacts from the API and renders them in a table.
    Redirects to /login if the session has no token.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    contacts: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/contacts", params={"limit": 100, "offset": 0})
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            contacts = payload.get("items", [])
            total = payload.get("total", len(contacts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/list.html",
        {
            "contacts": contacts,
            "total": total,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before the /{contact_id} route so FastAPI
# resolves /contacts/new as a literal path rather than a path parameter.
# ---------------------------------------------------------------------------


@router.get("/contacts/new", response_class=HTMLResponse, response_model=None)
async def contact_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-contact form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
        },
    )


@router.post("/contacts/new", response_class=HTMLResponse, response_model=None)
async def contact_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-contact form.

    Calls POST /api/v1/contacts on the upstream API.
    - 201 -> 303 redirect to /contacts/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with per-field errors + submitted values preserved
    - 401 -> clear session, redirect to /login
    - other errors -> re-render form with a generic error message
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    # Collect the raw submitted values for re-display on validation failure.
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the payload — only send non-empty optional fields.
    payload: dict[str, object] = {}
    for field in (
        "name",
        "contact_type",
        "email",
        "phone",
        "abn",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postcode",
        "country",
        "notes",
        "default_tax_code",
    ):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/contacts",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/contacts/{created['id']}", status_code=303)

    # 422 — parse per-field validation errors from FastAPI/Pydantic detail array.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    # Pydantic v2 location: ["body", "field_name"] or ["body", "field_name", ...]
                    loc = err.get("loc", [])
                    # Strip the leading "body" segment if present.
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
        "contacts/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: these routes MUST appear before /contacts/{contact_id} for the same
# literal-vs-parameter ordering reason as /contacts/new.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "name",
    "contact_type",
    "email",
    "phone",
    "abn",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postcode",
    "country",
    "notes",
    "default_tax_code",
)


@router.get("/contacts/{contact_id}/edit", response_class=HTMLResponse, response_model=None)
async def contact_edit_form(
    request: Request,
    contact_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing contact.

    Fetches the current record from the API and renders edit.html with all
    fields pre-filled. The current ``version`` is stored in a hidden input
    so the subsequent POST can include it in the ``If-Match`` header.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/contacts/{contact_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "contacts/edit.html",
            {"contact": None, "form": {}, "errors": {"__all__": "Contact not found"}, "conflict": False},
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "contacts/edit.html",
            {
                "contact": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
            },
            status_code=resp.status_code,
        )

    contact = resp.json()
    # Pre-populate the form dict from the API response.
    form: dict[str, str] = {field: str(contact.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(contact.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/edit.html",
        {
            "contact": contact,
            "form": form,
            "errors": {},
            "conflict": False,
        },
    )


@router.post("/contacts/{contact_id}/edit", response_class=HTMLResponse, response_model=None)
async def contact_update(
    request: Request,
    contact_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    Outcomes:
    - 200 OK      → 303 redirect to /contacts/{id}  (Post-Redirect-Get)
    - 409 Conflict → re-fetch latest record, re-render form with a conflict
                     banner and the server's current version in the hidden
                     input.  The user's submitted values are preserved so
                     they can compare and re-submit.
    - 422          → re-render with per-field validation errors
    - 428          → If-Match missing (shouldn't happen; log + generic error)
    - 401          → clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    # Build the PATCH payload — only include non-empty fields.
    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/contacts/{contact_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        return RedirectResponse(url=f"/contacts/{contact_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # and show a conflict banner so the user can reconcile their changes.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/contacts/{contact_id}")

        server_contact: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_contact.get("version", ""))

        # Keep user's submitted form values but update the hidden version to
        # the server's current version so the next submit has a fighting chance.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "contacts/edit.html",
            {
                "contact": server_contact,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_contact": server_contact,
            },
            status_code=409,
        )

    # 422 — parse per-field validation errors.
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
            "PATCH /api/v1/contacts/%s returned 428 — If-Match header was missing",
            contact_id,
        )
        errors["__all__"] = "Precondition required: version information was missing. Please reload and try again."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/edit.html",
        {
            "contact": None,
            "form": form,
            "errors": errors,
            "conflict": False,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/contacts/{contact_id}", response_class=HTMLResponse, response_model=None)
async def contact_detail(
    request: Request,
    contact_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single contact detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/contacts/{contact_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "contacts/detail.html",
                {"contact": None, "error": "Contact not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "contacts/detail.html",
                {"contact": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    contact = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/detail.html",
        {"contact": contact, "error": None},
    )
