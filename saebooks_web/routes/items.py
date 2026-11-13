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

import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client, optional_get
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


async def _form_refs(request: Request) -> dict[str, list[dict]]:
    """Accounts, GST codes and suppliers for the item form (batch 12)."""
    accounts = await _fetch_accounts(request)
    tax_codes: list[dict] = []
    suppliers: list[dict] = []
    async with api_client(request) as client:
        t = await optional_get(client, "/api/v1/tax_codes", {"limit": 200, "offset": 0})
        if t is not None:
            tax_codes = t.json().get("items", [])
        c = await optional_get(
            client, "/api/v1/contacts", {"type": "SUPPLIER", "limit": 500}
        )
        if c is not None:
            body = c.json()
            suppliers = body.get("items", []) if isinstance(body, dict) else []
    return {"accounts": accounts, "tax_codes": tax_codes, "suppliers": suppliers}


# Fields the create and edit forms send (batch 12 adds the purchase side,
# GST defaults and stock control).
_OPTIONAL_TEXT = ("description", "item_type", "cost_method", "unit_of_measure")
_NUMBER_FIELDS = ("default_sale_price", "purchase_price", "reorder_level")
_UUID_FIELDS = (
    "inventory_account_id", "cogs_account_id", "income_account_id",
    "purchase_account_id", "sale_tax_code_id", "purchase_tax_code_id",
    "preferred_supplier_id",
)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/items", response_class=HTMLResponse, response_model=None)
async def items_list(
    request: Request,
    item_type: str | None = None,
    stock: str | None = None,
    archived: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the items list page (full or HTMX fragment).

    Batch 12 (P2-16): filter by stock level and archived state, search,
    and show each item's stock value with the total for the filter."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if item_type:
        params["item_type"] = item_type
    if stock:
        params["stock"] = stock
    if archived:
        params["archived"] = archived
    if q:
        params["search"] = q
    total_value = None

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
            total_value = payload.get("total_value")
        elif resp.status_code == 404:
            # Feature not enabled on this edition
            error = "Items and inventory require the Offline edition or higher."
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
        "filter_stock": stock or "",
        "filter_archived": archived or "",
        "filter_q": q or "",
        "total_value": total_value,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "error": error,
        "flash": flash,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "items/_table.html" if is_htmx else "items/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Line-row picker data — GET /items/_options (batch 12, P0-42)
# NOTE: before /items/{item_id} so the literal path wins.
# ---------------------------------------------------------------------------

_OPTION_FIELDS = (
    "id", "sku", "name", "description", "item_type", "default_sale_price",
    "purchase_price", "income_account_id", "purchase_account_id",
    "inventory_account_id", "sale_tax_code_id", "purchase_tax_code_id",
    "unit_of_measure", "on_hand_qty",
)


@router.get("/items/_options", response_model=None)
async def items_options(request: Request) -> JSONResponse:
    """Active products and services for the line-row type-ahead. Empty
    when the inventory module is off, so the picker just stays empty."""
    if not _require_auth(request):
        return JSONResponse({"items": []}, status_code=401)
    async with api_client(request) as client:
        resp = await client.get("/api/v1/items", params={"limit": 1000, "offset": 0})
    if not resp.is_success:
        return JSONResponse({"items": []})
    rows = [
        {k: it.get(k) for k in _OPTION_FIELDS}
        for it in resp.json().get("items", [])
    ]
    return JSONResponse({"items": rows})


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{item_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/items/new", response_class=HTMLResponse, response_model=None)
async def item_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-item form with account dropdowns."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    refs = await _form_refs(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "items/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            **refs,
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

    for optional_field in (*_OPTIONAL_TEXT, *_NUMBER_FIELDS, *_UUID_FIELDS):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val
    if payload.get("item_type") == "service":
        # A service holds no stock: never send stock accounts for it.
        payload.pop("inventory_account_id", None)
        payload.pop("cogs_account_id", None)

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

    refs = await _form_refs(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "items/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            **refs,
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
    "purchase_price",
    "purchase_account_id",
    "sale_tax_code_id",
    "purchase_tax_code_id",
    "unit_of_measure",
    "reorder_level",
    "preferred_supplier_id",
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

    refs = await _form_refs(request)

    form: dict[str, str] = {field: str(item.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(item.get("version", ""))
    form["item_type"] = str(item.get("item_type") or "")

    return _TEMPLATES.TemplateResponse(
        request,
        "items/edit.html",
        {
            "item": item,
            "form": form,
            "errors": {},
            "conflict": False,
            **refs,
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

        refs = await _form_refs(request)

        return _TEMPLATES.TemplateResponse(
            request,
            "items/edit.html",
            {
                "item": server_item,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_item": server_item,
                **refs,
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

    refs = await _form_refs(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "items/edit.html",
        {
            "item": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            **refs,
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
# Restore — POST /{item_id}/restore (P2-16)
# ---------------------------------------------------------------------------


@router.post("/items/{item_id}/restore", response_class=HTMLResponse, response_model=None)
async def item_restore(request: Request, item_id: str) -> RedirectResponse:
    """Bring an archived item back."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form_data = await request.form()
    version = str(form_data.get("version", ""))
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/items/{item_id}/restore", headers={"If-Match": version}
        )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        request.session["flash"] = "Item restored."
    elif resp.status_code == 409:
        request.session["flash"] = "Someone changed this item. Reload and try again."
    else:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        request.session["flash"] = detail or f"Could not restore the item (HTTP {resp.status_code})."
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


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

        # Live stock levels for inventory items — GET /items/{id}/stock is
        # the canonical read (adds inventory_value = on_hand_qty * WAC). The
        # engine 404s it for service items and archived items; any failure
        # just hides the panel rather than erroring the page.
        stock = None
        movements: list[dict] = []
        if item.get("item_type") == "inventory" and not item.get("archived_at"):
            stock_resp = await client.get(f"/api/v1/items/{item_id}/stock")
            if stock_resp.is_success:
                stock = stock_resp.json()
        if item.get("item_type") == "inventory":
            # P1-66: movement history from the stock ledger.
            mv_resp = await optional_get(
                client, "/api/v1/stock/movements", {"item_id": item_id, "limit": 50}
            )
            if mv_resp is not None:
                movements = mv_resp.json().get("items", [])

    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "items/detail.html",
        {"item": item, "error": None, "flash": flash, "stock": stock,
         "movements": movements},
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

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/items/{item_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def item_hard_delete(request: Request, item_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/items",
        entity_id=item_id,
        entity_label=f"Item {item_id}",
        list_url="/items",
        detail_url=f"/items/{item_id}",
    )
