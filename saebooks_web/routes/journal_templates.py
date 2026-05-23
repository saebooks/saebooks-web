"""Journal Templates list, create, delete, and apply — Lane D cycle 46.

GET  /journal-templates              — list all non-archived templates
GET  /journal-templates/new          — blank create form (name + dynamic line rows)
POST /journal-templates/new          — submit create -> redirect to list on success
POST /journal-templates/{id}/delete  — soft-delete via DELETE to API -> redirect to list
GET  /journal-templates/{id}/apply   — redirect to /journal-entries/new with prefill params

Route ordering: /new + /{id}/apply + /{id}/delete MUST appear before the catch-all
path so FastAPI matches literal paths first.

API calls:
- GET  /api/v1/journal_templates
- POST /api/v1/journal_templates
- DELETE /api/v1/journal_templates/{id} with If-Match header
- POST /api/v1/journal_templates/{id}/apply

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import urllib.parse
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


@router.get("/journal-templates", response_class=HTMLResponse, response_model=None)
async def journal_templates_list(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Render the journal templates list page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    templates: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/journal_templates")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            # Support both paginated {"items": [...]} and plain list responses.
            if isinstance(payload, list):
                templates = payload
            else:
                templates = payload.get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_templates/list.html",
        {
            "templates": templates,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Create — GET (blank form) + POST (submit)
# NOTE: MUST appear before /{template_id} paths.
# ---------------------------------------------------------------------------


@router.get("/journal-templates/new", response_class=HTMLResponse, response_model=None)
async def journal_template_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the blank journal template create form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "journal_templates/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            # Two starter blank lines (same pattern as journal entries).
            "line_count": 2,
        },
    )


@router.post("/journal-templates/new", response_class=HTMLResponse, response_model=None)
async def journal_template_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-journal-template form.

    - 201 -> 303 redirect to /journal-templates
    - 422 -> re-render form with per-field errors
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the payload — collect dynamic line fields.
    # Form sends: lines[0][account_id], lines[0][description], lines[0][debit], lines[0][credit], ...
    lines: list[dict] = []
    idx = 0
    while True:
        account_id = form.get(f"lines[{idx}][account_id]", "").strip()
        if not account_id and idx > 0:
            break
        if account_id:
            line: dict = {"account_id": account_id}
            desc = form.get(f"lines[{idx}][description]", "").strip()
            if desc:
                line["description"] = desc
            debit = form.get(f"lines[{idx}][debit]", "").strip()
            credit = form.get(f"lines[{idx}][credit]", "").strip()
            line["debit"] = debit or "0"
            line["credit"] = credit or "0"
            lines.append(line)
        idx += 1
        if idx > 50:
            break

    name = form.get("name", "").strip()
    payload: dict = {"name": name}
    if lines:
        payload["lines"] = lines

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/journal_templates",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        request.session["flash"] = f"Journal template \"{name}\" created."
        return RedirectResponse(url="/journal-templates", status_code=303)

    # 422 or other error — re-render.
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
        "journal_templates/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "line_count": max(len(lines), 2),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Apply — GET /{template_id}/apply
# Calls POST /api/v1/journal_templates/{id}/apply to retrieve prefill data,
# then redirects to /journal-entries/new with query params.
# NOTE: MUST appear before /{template_id}/delete.
# ---------------------------------------------------------------------------


@router.get(
    "/journal-templates/{template_id}/apply",
    response_class=HTMLResponse,
    response_model=None,
)
async def journal_template_apply(
    request: Request,
    template_id: str,
) -> RedirectResponse:
    """Fetch prefill data from the API and redirect to the journal entry new form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/journal_templates/{template_id}/apply")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if not resp.is_success:
        request.session["flash"] = f"Could not apply template: HTTP {resp.status_code}"
        return RedirectResponse(url="/journal-templates", status_code=303)

    prefill = resp.json()

    # Build query string: ?from_template={id}&line[0][account_id]=...&...
    params: list[tuple[str, str]] = [("from_template", template_id)]
    for i, line in enumerate(prefill.get("lines", [])):
        for key in ("account_id", "description", "debit", "credit"):
            val = line.get(key)
            if val is not None:
                params.append((f"lines[{i}][{key}]", str(val)))

    qs = urllib.parse.urlencode(params)
    return RedirectResponse(url=f"/journal-entries/new?{qs}", status_code=303)


# ---------------------------------------------------------------------------
# Delete — POST /{template_id}/delete
# NOTE: MUST appear before the catch-all /{template_id}.
# ---------------------------------------------------------------------------


@router.post(
    "/journal-templates/{template_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def journal_template_delete(
    request: Request,
    template_id: str,
) -> RedirectResponse:
    """Soft-delete a journal template via DELETE /api/v1/journal_templates/{id}.

    Reads version from the POST body for If-Match header.
    On success redirects to /journal-templates with a flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/journal_templates/{template_id}",
            headers={"If-Match": version} if version else {},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success or resp.status_code == 204:
        request.session["flash"] = "Journal template deleted."
    elif resp.status_code == 409:
        request.session["flash"] = "Delete failed: version conflict. Please retry."
    else:
        request.session["flash"] = f"Delete failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/journal-templates", status_code=303)


# ---------------------------------------------------------------------------
# Bulk action — POST /journal-templates/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_JOURNAL_TEMPLATES = {
    "delete": ("DELETE", "/api/v1/journal_templates/{id}"),
}


@router.post("/journal-templates/bulk", response_class=HTMLResponse, response_model=None)
async def journal_templates_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many journal templates at once.

    Form fields:
      action  — one of: delete
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /journal-templates. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_JOURNAL_TEMPLATES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/journal-templates", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/journal-templates", status_code=303)

    method, path_tpl = _BULK_ACTIONS_JOURNAL_TEMPLATES[action]
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
        request.session["flash"] = f"{label}: {ok} journal template{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/journal-templates", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/journal-templates/{template_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def journal_template_hard_delete(request: Request, template_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/journal_templates",
        entity_id=template_id,
        entity_label=f"Journal template {template_id}",
        list_url="/journal-templates",
        detail_url=f"/journal-templates/{template_id}",
    )
