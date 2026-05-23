"""Items list, detail, create and edit views — Lane D cycles 9 + 22.

GET  /items              — list page (HTMX-aware, limit/offset pagination)
GET  /items/new          — empty create form; generates idempotency key
POST /items/new          — submit to upstream API; redirect on success,
                          re-render with errors on 422
GET  /items/{id}         — item detail (flash from session)
GET  /items/{id}/edit    — pre-populated edit form (version in hidden input)
                          If item is archived → 422 + edit_blocked.html
POST /items/{id}/edit    — submit PATCH to API with If-Match; redirect on
                          success, re-render on 409 (conflict) or 422
POST /items/{id}/archive — soft-archive via archive_entity helper

Route ordering: /new + /{id}/edit + /{id}/archive MUST appear before the
catch-all /{id} GET so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import asyncio
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
# List
# ---------------------------------------------------------------------------


@router.get("/items", response_class=HTMLResponse, response_model=None)
async def items_list(
    request: Request,
    item_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the items list page (full or HTMX fragment)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if item_type:
        params["item_type"] = item_type

    error: str | None = None
    items: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/items", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            items = payload.get("items", [])
            total = payload.get("total", len(items))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset: int | None = offset - limit if offset > 0 else None
    next_offset: int | None = offset + limit if offset + limit < total else None

    # Consume and clear any flash message (e.g. from a successful archive/edit).
    flash = request.session.pop("flash", None)

    ctx = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filter_item_type": item_type or "",
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "error": error,
        "flash": flash,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "items/_table.html" if is_htmx else "items/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{item_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/items/new", response_class=HTMLResponse, response_model=None)
async def item_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-item form with account dropdowns."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts = await _fetch_accounts(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "items/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
        },
    )


@router.post("/items/new", response_class=HTMLResponse, response_model=None)
async def item_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-item form.

    - 201 -> 303 redirect to /items/{id}
    - 422 -> re-render form with per-field errors (or __all__ for string errors)
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the payload.  Required fields are always included; optional fields
    # only when non-empty.
    payload: dict[str, object] = {}

    for required_field in ("sku", "name"):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    for optional_field in (
        "description",
        "item_type",
        "cost_method",
    ):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    for price_field in ("default_sale_price",):
        val = form.get(price_field, "").strip()
        if val:
            payload[price_field] = val

    for uuid_field in ("inventory_account_id", "cogs_account_id", "income_account_id"):
        val = form.get(uuid_field, "").strip()
        if val:
            payload[uuid_field] = val

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/items",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/items/{created['id']}", status_code=303)

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
        "items/new.html",
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
# NOTE: MUST appear before /{item_id} catch-all.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "sku",
    "name",
    "description",
    "default_sale_price",
    "inventory_account_id",
    "cogs_account_id",
    "income_account_id",
)


@router.get("/items/{item_id}/edit", response_class=HTMLResponse, response_model=None)
async def item_edit_form(
    request: Request,
    item_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing item.

    If the item is already archived, renders edit_blocked.html with HTTP 422.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/items/{item_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "items/edit.html",
            {"item": None, "form": {}, "errors": {"__all__": "Item not found"}, "conflict": False, "accounts": []},
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "items/edit.html",
            {
                "item": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "accounts": [],
            },
            status_code=resp.status_code,
        )

    item = resp.json()

    # Block editing archived items.
    if item.get("archived_at"):
        return _TEMPLATES.TemplateResponse(
            request,
            "items/edit_blocked.html",
            {"item": item},
            status_code=422,
        )

    accounts = await _fetch_accounts(request)

    form: dict[str, str] = {field: str(item.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(item.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "items/edit.html",
        {
            "item": item,
            "form": form,
            "errors": {},
            "conflict": False,
            "accounts": accounts,
        },
    )


@router.post("/items/{item_id}/edit", response_class=HTMLResponse, response_model=None)
async def item_update(
    request: Request,
    item_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK      -> 303 redirect to /items/{id}
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
            f"/api/v1/items/{item_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Item saved."
        return RedirectResponse(url=f"/items/{item_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/items/{item_id}")

        server_item: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_item.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        accounts = await _fetch_accounts(request)

        return _TEMPLATES.TemplateResponse(
            request,
            "items/edit.html",
            {
                "item": server_item,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_item": server_item,
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
            "PATCH /api/v1/items/%s returned 428 — If-Match header was missing",
            item_id,
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
        "items/edit.html",
        {
            "item": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "accounts": accounts,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{item_id}/archive
# NOTE: MUST appear before the catch-all /{item_id} GET.
# ---------------------------------------------------------------------------


@router.post("/items/{item_id}/archive", response_class=HTMLResponse, response_model=None)
async def item_archive(
    request: Request,
    item_id: str,
) -> RedirectResponse:
    """Soft-archive an item via DELETE /api/v1/items/{id} with If-Match.

    On success redirects to /items with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/items",
        entity_id=item_id,
        version=str(version),
        entity_label=f"Item {item_id}",
        list_url="/items",
        detail_url=f"/items/{item_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/items/{item_id}", response_class=HTMLResponse, response_model=None)
async def item_detail(
    request: Request,
    item_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single item detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/items/{item_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "items/detail.html",
                {"item": None, "error": "Item not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "items/detail.html",
                {"item": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    item = resp.json()
    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "items/detail.html",
        {"item": item, "error": None, "flash": flash},
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /items/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_ITEMS = {
    "archive": ("DELETE", "/api/v1/items/{id}"),
}


@router.post("/items/bulk", response_class=HTMLResponse, response_model=None)
async def items_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many items at once.

    Form fields:
      action  — one of: archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /items. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_ITEMS:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/items", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/items", status_code=303)

    method, path_tpl = _BULK_ACTIONS_ITEMS[action]
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
        request.session["flash"] = f"{label}: {ok} item{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/items", status_code=303)
