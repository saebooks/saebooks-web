"""Bank rules list, create, edit, delete, and apply-all — Lane D cycle 47.

GET  /bank-rules                — list all bank rules
GET  /bank-rules/new            — create form
POST /bank-rules/new            — submit -> redirect to list on 201
GET  /bank-rules/{id}/edit      — edit form pre-filled
POST /bank-rules/{id}/edit      — submit PATCH -> redirect on 200, conflict banner on 409
POST /bank-rules/{id}/delete    — DELETE -> redirect to list
POST /bank-rules/apply-all      — trigger bulk apply -> redirect with flash

Route ordering: /new + /apply-all MUST appear before /{rule_id} paths so
FastAPI matches literal paths first.

API calls:
- GET   /api/v1/bank_rules
- POST  /api/v1/bank_rules
- GET   /api/v1/bank_rules/{id}
- PATCH /api/v1/bank_rules/{id} with If-Match
- DELETE /api/v1/bank_rules/{id} with If-Match
- POST  /api/v1/bank_rules/apply

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

# Fields sent in create/edit payload
_EDIT_FIELDS = (
    "name",
    "match_field",
    "match_operator",
    "match_value",
    "action_account_id",
    "action_tax_code_id",
    "priority",
)


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _parse_errors(resp_json: dict) -> dict[str, str]:
    """Parse a 422 response body into a field->message error dict."""
    errors: dict[str, str] = {}
    try:
        detail = resp_json.get("detail", [])
        if isinstance(detail, list):
            for err in detail:
                loc = err.get("loc", [])
                field_parts = [p for p in loc if p != "body"]
                field = str(field_parts[0]) if field_parts else "__all__"
                errors[field] = err.get("msg", "Invalid value")
        elif isinstance(detail, str):
            errors["__all__"] = detail
    except Exception:
        errors["__all__"] = "Validation error"
    return errors


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/bank-rules", response_class=HTMLResponse, response_model=None)
async def bank_rules_list(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the bank rules list page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    rules: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank_rules")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            if isinstance(payload, list):
                rules = payload
            else:
                rules = payload.get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/list.html",
        {
            "rules": rules,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Create — GET (blank form) + POST (submit)
# NOTE: MUST appear before /{rule_id} paths.
# ---------------------------------------------------------------------------


@router.get("/bank-rules/new", response_class=HTMLResponse, response_model=None)
async def bank_rule_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the blank bank rule create form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/new.html",
        {
            "form": {},
            "errors": {},
        },
    )


@router.post("/bank-rules/new", response_class=HTMLResponse, response_model=None)
async def bank_rule_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-bank-rule form.

    - 201 -> 303 redirect to /bank-rules
    - 422 -> re-render form with per-field errors
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    payload: dict[str, object] = {}
    for field in ("name", "match_field", "match_operator", "match_value", "action_account_id"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val
    # Optional fields
    tax_code = form.get("action_tax_code_id", "").strip()
    if tax_code:
        payload["action_tax_code_id"] = tax_code
    priority_raw = form.get("priority", "").strip()
    if priority_raw:
        try:
            payload["priority"] = int(priority_raw)
        except ValueError:
            payload["priority"] = priority_raw

    async with api_client(request) as client:
        resp = await client.post("/api/v1/bank_rules", json=payload)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        request.session["flash"] = "Bank rule created."
        return RedirectResponse(url="/bank-rules", status_code=303)

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_errors(resp.json())
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/new.html",
        {"form": form, "errors": errors},
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Apply all — POST /bank-rules/apply-all
# NOTE: MUST appear before /{rule_id} paths.
# ---------------------------------------------------------------------------


@router.post("/bank-rules/apply-all", response_class=HTMLResponse, response_model=None)
async def bank_rules_apply_all(request: Request) -> RedirectResponse:
    """Trigger bulk apply of all bank rules.

    POSTs to POST /api/v1/bank_rules/apply and redirects to the list
    with a flash message showing how many lines were categorised.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post("/api/v1/bank_rules/apply")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        try:
            data = resp.json()
            applied = data.get("applied", data.get("count", ""))
            if applied != "":
                request.session["flash"] = f"Bank rules applied — {applied} line(s) categorised."
            else:
                request.session["flash"] = "Bank rules applied."
        except Exception:
            request.session["flash"] = "Bank rules applied."
    else:
        request.session["flash"] = f"Apply failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/bank-rules", status_code=303)


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear after /new and /apply-all.
# ---------------------------------------------------------------------------


@router.get(
    "/bank-rules/{rule_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_rule_edit_form(
    request: Request,
    rule_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing bank rule."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_rules/{rule_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_rules/edit.html",
            {
                "rule": None,
                "form": {},
                "errors": {"__all__": "Bank rule not found"},
                "conflict": False,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_rules/edit.html",
            {
                "rule": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
            },
            status_code=resp.status_code,
        )

    rule_obj = resp.json()
    form: dict[str, str] = {field: str(rule_obj.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(rule_obj.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/edit.html",
        {
            "rule": rule_obj,
            "form": form,
            "errors": {},
            "conflict": False,
        },
    )


@router.post(
    "/bank-rules/{rule_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_rule_update(
    request: Request,
    rule_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /bank-rules
    - 409 Conflict -> re-fetch latest, re-render with conflict banner
    - 422          -> re-render with per-field validation errors
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    payload: dict[str, object] = {}
    for field in ("name", "match_field", "match_operator", "match_value", "action_account_id"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val
    tax_code = form.get("action_tax_code_id", "").strip()
    if tax_code:
        payload["action_tax_code_id"] = tax_code
    priority_raw = form.get("priority", "").strip()
    if priority_raw:
        try:
            payload["priority"] = int(priority_raw)
        except ValueError:
            payload["priority"] = priority_raw

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/bank_rules/{rule_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Bank rule saved."
        return RedirectResponse(url="/bank-rules", status_code=303)

    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/bank_rules/{rule_id}")
        server_rule: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_rule.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "bank_rules/edit.html",
            {
                "rule": server_rule,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
            },
            status_code=409,
        )

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_errors(resp.json())
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/edit.html",
        {
            "rule": None,
            "form": form,
            "errors": errors,
            "conflict": False,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Delete — POST /{rule_id}/delete
# ---------------------------------------------------------------------------


@router.post(
    "/bank-rules/{rule_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_rule_delete(
    request: Request,
    rule_id: str,
) -> RedirectResponse:
    """Delete a bank rule via DELETE /api/v1/bank_rules/{id} with If-Match."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/bank_rules/{rule_id}",
            headers={"If-Match": version} if version else {},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success or resp.status_code == 204:
        request.session["flash"] = "Bank rule deleted."
    elif resp.status_code == 409:
        request.session["flash"] = "Delete failed: version conflict. Please retry."
    else:
        request.session["flash"] = f"Delete failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/bank-rules", status_code=303)
