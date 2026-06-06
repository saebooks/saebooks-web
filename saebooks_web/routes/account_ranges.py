"""Account Ranges list, create, edit, delete, and prefix mode — Lane D cycle 46.

GET  /admin/ranges                  — list all ranges + current prefix mode
GET  /admin/ranges/new              — create form
POST /admin/ranges/new              — submit -> redirect to list
GET  /admin/ranges/{id}/edit        — edit form
POST /admin/ranges/{id}/edit        — submit PATCH -> redirect
POST /admin/ranges/{id}/delete      — DELETE -> redirect
POST /admin/ranges/prefix_mode      — update prefix mode setting -> redirect

Route ordering: /new + /prefix_mode MUST appear before /{range_id} paths so
FastAPI matches literal paths first.

API calls:
- GET   /api/v1/account_ranges
- GET   /api/v1/account_ranges/prefix_mode
- POST  /api/v1/account_ranges
- PATCH /api/v1/account_ranges/{id} with If-Match
- DELETE /api/v1/account_ranges/{id} with If-Match
- PATCH /api/v1/account_ranges/prefix_mode

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

# Valid prefix mode values exposed in the UI.
PREFIX_MODES = [
    ("none", "None"),
    ("first_digit", "First digit"),
    ("first_two_digits", "First two digits"),
    ("custom", "Custom"),
]

_EDIT_FIELDS = (
    "name",
    "prefix",
    "account_type",
    "description",
)


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _require_admin(request: Request) -> bool:
    """True if session is SAE staff or tenant admin."""
    role = request.session.get("user_role", "")
    is_staff = bool(request.session.get("is_sae_staff"))
    return is_staff or role == "admin"


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


@router.get("/admin/ranges", response_class=HTMLResponse, response_model=None)
async def account_ranges_list(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Render the account ranges list page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    error: str | None = None
    ranges: list[dict] = []
    prefix_mode: str = "none"

    async with api_client(request) as client:
        ranges_resp = await client.get("/api/v1/account_ranges")
        mode_resp = await client.get("/api/v1/account_ranges/prefix_mode")

        if ranges_resp.status_code == 401 or mode_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if ranges_resp.is_success:
            payload = ranges_resp.json()
            ranges = payload if isinstance(payload, list) else payload.get("items", [])
        else:
            error = f"API error: HTTP {ranges_resp.status_code}"

        if mode_resp.is_success:
            mode_payload = mode_resp.json()
            prefix_mode = mode_payload.get("prefix_mode", "none")

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "account_ranges/list.html",
        {
            "ranges": ranges,
            "prefix_mode": prefix_mode,
            "prefix_modes": PREFIX_MODES,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Create — GET (blank form) + POST (submit)
# NOTE: MUST appear before /{range_id} paths.
# ---------------------------------------------------------------------------


@router.get("/admin/ranges/new", response_class=HTMLResponse, response_model=None)
async def account_range_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the blank account range create form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    return _TEMPLATES.TemplateResponse(
        request,
        "account_ranges/new.html",
        {
            "form": {},
            "errors": {},
        },
    )


@router.post("/admin/ranges/new", response_class=HTMLResponse, response_model=None)
async def account_range_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-account-range form.

    - 201 -> 303 redirect to /admin/ranges
    - 422 -> re-render form with per-field errors
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    payload: dict[str, object] = {}
    for field in ("name", "prefix", "account_type"):
        val = form.get(field, "").strip()
        payload[field] = val
    desc = form.get("description", "").strip()
    if desc:
        payload["description"] = desc

    async with api_client(request) as client:
        resp = await client.post("/api/v1/account_ranges", json=payload)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        request.session["flash"] = "Account range created."
        return RedirectResponse(url="/admin/ranges", status_code=303)

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_errors(resp.json())
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "account_ranges/new.html",
        {"form": form, "errors": errors},
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Prefix mode update — POST /admin/ranges/prefix_mode
# NOTE: MUST appear before /{range_id} paths.
# ---------------------------------------------------------------------------


@router.post(
    "/admin/ranges/prefix_mode",
    response_class=HTMLResponse,
    response_model=None,
)
async def account_ranges_update_prefix_mode(
    request: Request,
) -> RedirectResponse:
    """Update the account prefix mode setting."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    prefix_mode = str(form_data.get("prefix_mode", "none")).strip()

    async with api_client(request) as client:
        resp = await client.patch(
            "/api/v1/account_ranges/prefix_mode",
            json={"prefix_mode": prefix_mode},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = "Prefix mode updated."
    else:
        request.session["flash"] = f"Failed to update prefix mode: HTTP {resp.status_code}"

    return RedirectResponse(url="/admin/ranges", status_code=303)


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before the catch-all /{range_id} (if any).
# ---------------------------------------------------------------------------


@router.get(
    "/admin/ranges/{range_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def account_range_edit_form(
    request: Request,
    range_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing account range."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/account_ranges/{range_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "account_ranges/edit.html",
            {
                "range": None,
                "form": {},
                "errors": {"__all__": "Account range not found"},
                "conflict": False,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "account_ranges/edit.html",
            {
                "range": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
            },
            status_code=resp.status_code,
        )

    range_obj = resp.json()
    form: dict[str, str] = {field: str(range_obj.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(range_obj.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "account_ranges/edit.html",
        {
            "range": range_obj,
            "form": form,
            "errors": {},
            "conflict": False,
        },
    )


@router.post(
    "/admin/ranges/{range_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def account_range_update(
    request: Request,
    range_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /admin/ranges
    - 409 Conflict -> re-fetch latest, re-render with conflict banner
    - 422          -> re-render with per-field validation errors
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/account_ranges/{range_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Account range saved."
        return RedirectResponse(url="/admin/ranges", status_code=303)

    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/account_ranges/{range_id}")
        server_range: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_range.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "account_ranges/edit.html",
            {
                "range": server_range,
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
        "account_ranges/edit.html",
        {
            "range": None,
            "form": form,
            "errors": errors,
            "conflict": False,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Delete — POST /{range_id}/delete
# ---------------------------------------------------------------------------


@router.post(
    "/admin/ranges/{range_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def account_range_delete(
    request: Request,
    range_id: str,
) -> RedirectResponse:
    """Delete an account range via DELETE /api/v1/account_ranges/{id} with If-Match."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/account_ranges/{range_id}",
            headers={"If-Match": version} if version else {},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success or resp.status_code == 204:
        request.session["flash"] = "Account range deleted."
    elif resp.status_code == 409:
        request.session["flash"] = "Delete failed: version conflict. Please retry."
    else:
        request.session["flash"] = f"Delete failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/admin/ranges", status_code=303)


# ---------------------------------------------------------------------------
# Bulk action — POST /admin/ranges/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_ACCOUNT_RANGES = {
    "delete": ("DELETE", "/api/v1/account_ranges/{id}"),
}


@router.post("/admin/ranges/bulk", response_class=HTMLResponse, response_model=None)
async def account_ranges_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many account ranges at once.

    Form fields:
      action  — one of: delete
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /admin/ranges. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_ACCOUNT_RANGES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/admin/ranges", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/admin/ranges", status_code=303)

    method, path_tpl = _BULK_ACTIONS_ACCOUNT_RANGES[action]
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
        request.session["flash"] = f"{label}: {ok} account range{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/admin/ranges", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/admin/ranges/{range_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def account_range_hard_delete(request: Request, range_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/account_ranges",
        entity_id=range_id,
        entity_label=f"Account range {range_id}",
        list_url="/admin/ranges",
        detail_url=f"/admin/ranges/{range_id}",
    )
