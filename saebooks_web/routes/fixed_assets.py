"""Fixed assets list, detail, create, edit, dispose, archive and post-depreciation views.

Lane D cycles 26 + 31 + 33 + 40 + 42.

GET  /fixed-assets/new                    — empty create form
POST /fixed-assets/new                    — submit to API; 303 on success, 422 re-render on error
GET  /fixed-assets/{id}/edit              — pre-populated edit form
                                            If status == DISPOSED → edit_blocked.html (HTTP 422)
POST /fixed-assets/{id}/edit              — PATCH with If-Match; 303 with flash on success
POST /fixed-assets/{id}/dispose           — POST to /api/v1/fixed_assets/{id}/dispose with If-Match
POST /fixed-assets/{id}/archive           — soft-archive via archive_entity helper
POST /fixed-assets/{id}/post-depreciation — POST to /api/v1/fixed_assets/{id}/post_depreciation
GET  /fixed-assets/depreciation-run       — batch depreciation run form
POST /fixed-assets/depreciation-run       — submit batch run; renders results inline (no redirect)
GET  /fixed-assets                        — list page (paginated, HTMX-aware)
GET  /fixed-assets/{id}                   — fixed asset detail

Route ordering: /new + /{id}/edit + /{id}/dispose + /{id}/archive + /{id}/post-depreciation
+ /depreciation-run MUST appear before /{id} catch-all so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.

Cycle 33: depreciation_model_id uses a <select> populated from
GET /api/v1/depreciation_models (6 seeded rows).
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date
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


async def _fetch_dep_models(request: Request) -> list[dict]:
    """Fetch depreciation models for the select dropdown (max 100).

    Returns an empty list on any API error — the template shows a graceful
    fallback in that case.
    """
    async with api_client(request) as client:
        resp = await client.get("/api/v1/depreciation_models", params={"limit": 100})
    if resp.status_code == 200:
        return resp.json().get("items", [])
    return []


def _parse_422(body: dict) -> dict[str, str]:
    """Extract field -> message errors from a 422 response body."""
    errors: dict[str, str] = {}
    try:
        detail = body.get("detail", [])
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
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{asset_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/fixed-assets/new", response_class=HTMLResponse, response_model=None)
async def fixed_asset_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-asset form with account dropdowns."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts = await _fetch_accounts(request)
    dep_models = await _fetch_dep_models(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
            "dep_models": dep_models,
        },
    )


@router.post("/fixed-assets/new", response_class=HTMLResponse, response_model=None)
async def fixed_asset_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-asset form.

    - 201 -> 303 redirect to /fixed-assets/{id}
    - 422 -> re-render form with per-field errors
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Required fields.
    payload: dict[str, object] = {}
    for required_field in (
        "name",
        "depreciation_model_id",
        "cost_account_id",
        "accum_dep_account_id",
        "dep_expense_account_id",
        "purchase_date",
        "cost",
    ):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    # Optional fields — only include when non-empty.
    for optional_field in (
        "description",
        "code",
        "tax_model_id",
        "serial_number",
        "manufacturer",
        "model_number",
        "location",
        "custody_person",
        "warranty_end",
        "in_service_date",
    ):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    residual_value = form.get("residual_value", "").strip()
    if residual_value:
        payload["residual_value"] = residual_value

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/fixed_assets",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/fixed-assets/{created['id']}", status_code=303)

    # 422 — parse per-field or plain-string errors.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_422(resp.json())
        if not errors:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts = await _fetch_accounts(request)
    dep_models = await _fetch_dep_models(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "accounts": accounts,
            "dep_models": dep_models,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before /{asset_id} catch-all.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "name",
    "description",
    "depreciation_model_id",
    "tax_model_id",
    "purchase_date",
    "in_service_date",
    "residual_value",
    "serial_number",
    "manufacturer",
    "model_number",
    "location",
    "custody_person",
    "warranty_end",
)


@router.get("/fixed-assets/{asset_id}/edit", response_class=HTMLResponse, response_model=None)
async def fixed_asset_edit_form(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing fixed asset.

    If the asset has status DISPOSED, renders edit_blocked.html with HTTP 422.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/fixed_assets/{asset_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "fixed_assets/edit.html",
            {
                "asset": None,
                "form": {},
                "errors": {"__all__": "Fixed asset not found"},
                "conflict": False,
                "accounts": [],
                "dep_models": [],
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "fixed_assets/edit.html",
            {
                "asset": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "accounts": [],
                "dep_models": [],
            },
            status_code=resp.status_code,
        )

    asset = resp.json()

    # Block editing disposed assets.
    if asset.get("status") == "DISPOSED":
        return _TEMPLATES.TemplateResponse(
            request,
            "fixed_assets/edit_blocked.html",
            {"asset": asset},
            status_code=422,
        )

    accounts = await _fetch_accounts(request)
    dep_models = await _fetch_dep_models(request)

    form: dict[str, str] = {field: str(asset.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(asset.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/edit.html",
        {
            "asset": asset,
            "form": form,
            "errors": {},
            "conflict": False,
            "accounts": accounts,
            "dep_models": dep_models,
        },
    )


@router.post("/fixed-assets/{asset_id}/edit", response_class=HTMLResponse, response_model=None)
async def fixed_asset_update(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK      -> 303 redirect to /fixed-assets/{id}
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
            f"/api/v1/fixed_assets/{asset_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Fixed asset saved."
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/fixed_assets/{asset_id}")

        server_asset: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_asset.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        accounts = await _fetch_accounts(request)
        dep_models = await _fetch_dep_models(request)

        return _TEMPLATES.TemplateResponse(
            request,
            "fixed_assets/edit.html",
            {
                "asset": server_asset,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_asset": server_asset,
                "accounts": accounts,
                "dep_models": dep_models,
            },
            status_code=409,
        )

    # 422 — per-field validation errors.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_422(resp.json())
        if not errors:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    elif resp.status_code == 428:
        import logging as _logging

        _logging.getLogger(__name__).error(
            "PATCH /api/v1/fixed_assets/%s returned 428 — If-Match header was missing",
            asset_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts = await _fetch_accounts(request)
    dep_models = await _fetch_dep_models(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/edit.html",
        {
            "asset": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "accounts": accounts,
            "dep_models": dep_models,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Dispose — POST /{asset_id}/dispose
# NOTE: MUST appear before the catch-all /{asset_id} GET.
# ---------------------------------------------------------------------------


@router.post("/fixed-assets/{asset_id}/dispose", response_class=HTMLResponse, response_model=None)
async def fixed_asset_dispose(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the dispose form — POST to /api/v1/fixed_assets/{id}/dispose.

    - 200 OK  -> 303 redirect to /fixed-assets/{id} with flash
    - 422     -> 303 redirect back to detail with flash error
    - 409     -> 303 redirect back to detail with conflict flash
    - 401     -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    payload: dict[str, object] = {}
    for field in ("disposal_date", "proceeds"):
        val = form.get(field, "").strip()
        payload[field] = val

    notes = form.get("notes", "").strip()
    if notes:
        payload["notes"] = notes

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/fixed_assets/{asset_id}/dispose",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Fixed asset disposed."
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Dispose failed — asset was modified. Refresh and try again."
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    # 422 or other error.
    flash_msg = f"Dispose failed (HTTP {resp.status_code})."
    try:
        detail = resp.json().get("detail", "")
        if isinstance(detail, str) and detail:
            flash_msg = detail
        elif isinstance(detail, list) and detail:
            flash_msg = detail[0].get("msg", flash_msg)
    except Exception:
        pass
    request.session["flash"] = flash_msg
    return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------------
# Archive — POST /{asset_id}/archive
# NOTE: MUST appear before the catch-all /{asset_id} GET.
# ---------------------------------------------------------------------------


@router.post("/fixed-assets/{asset_id}/archive", response_class=HTMLResponse, response_model=None)
async def fixed_asset_archive(
    request: Request,
    asset_id: str,
) -> RedirectResponse:
    """Soft-archive a fixed asset via DELETE /api/v1/fixed_assets/{id} with If-Match.

    On success redirects to /fixed-assets with a flash.
    On 409 or 422 redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/fixed_assets",
        entity_id=asset_id,
        version=str(version),
        entity_label=f"Fixed asset {asset_id}",
        list_url="/fixed-assets",
        detail_url=f"/fixed-assets/{asset_id}",
    )


# ---------------------------------------------------------------------------
# Post Depreciation — POST /{asset_id}/post-depreciation
# NOTE: MUST appear before the catch-all /{asset_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/fixed-assets/{asset_id}/post-depreciation",
    response_class=HTMLResponse,
    response_model=None,
)
async def fixed_asset_post_depreciation(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Post depreciation for a fixed asset through a given date.

    Reads ``through_date`` and ``version`` from the form body and calls
    POST /api/v1/fixed_assets/{id}/post_depreciation with an If-Match header.

    - 200 OK  -> 303 redirect to /fixed-assets/{id} with flash (amount posted)
    - 409     -> 303 redirect back with conflict flash
    - 422     -> 303 redirect back with API error detail flash
    - 401     -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    through_date = form.get("through_date", "").strip()
    version = form.get("version", "").strip()

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/fixed_assets/{asset_id}/post_depreciation",
            json={"through": through_date},
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        body = resp.json()
        amount_posted = body.get("amount_posted", 0)
        if amount_posted and float(amount_posted) > 0:
            request.session["flash"] = (
                f"Depreciation posted: ${float(amount_posted):.2f} through {through_date}"
            )
        else:
            request.session["flash"] = "No depreciation to post (already up to date)"
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — reload and try again"
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    # 422 or other error — extract detail string if available.
    flash_msg = f"Post depreciation failed (HTTP {resp.status_code})."
    try:
        detail = resp.json().get("detail", "")
        if isinstance(detail, str) and detail:
            flash_msg = detail
        elif isinstance(detail, list) and detail:
            flash_msg = detail[0].get("msg", flash_msg)
    except Exception:
        pass
    request.session["flash"] = flash_msg
    return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------------
# Convert to Inventory — POST /{asset_id}/convert-to-inventory
# Gap MOTR-3: demonstrator FA → used-vehicle inventory stock.
# NOTE: MUST appear before the catch-all /{asset_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/fixed-assets/{asset_id}/convert-to-inventory",
    response_class=HTMLResponse,
    response_model=None,
)
async def fixed_asset_convert_to_inventory(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the convert-to-inventory form.

    POSTs to /api/v1/fixed_assets/{id}/convert_to_inventory with If-Match.

    - 201 OK  -> 303 redirect to /fixed-assets/{id} with flash (NBV, item SKU)
    - 409     -> 303 redirect back with conflict flash
    - 422     -> 303 redirect back with API error detail flash
    - 401     -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "").strip()

    payload: dict[str, object] = {}
    for field in ("conversion_date", "inventory_account_id", "cogs_account_id", "income_account_id"):
        val = form.get(field, "").strip()
        payload[field] = val

    for optional_field in ("sku", "vin"):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        body = resp.json()
        nbv = float(body.get("nbv", 0))
        item_sku = body.get("item_sku", "")
        request.session["flash"] = (
            f"Demonstrator converted to inventory — item {item_sku} created at NBV ${nbv:.2f}. "
            f"FA marked disposed."
        )
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Conversion failed — asset was modified. Refresh and try again."
        return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)

    # 422 or other error — extract detail string if available.
    flash_msg = f"Conversion failed (HTTP {resp.status_code})."
    try:
        detail = resp.json().get("detail", "")
        if isinstance(detail, str) and detail:
            flash_msg = detail
        elif isinstance(detail, list) and detail:
            flash_msg = detail[0].get("msg", flash_msg)
    except Exception:
        pass
    request.session["flash"] = flash_msg
    return RedirectResponse(url=f"/fixed-assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------------
# Batch Depreciation Run — GET (form) + POST (submit, renders results inline)
# NOTE: MUST appear before the catch-all /{asset_id} GET.
# ---------------------------------------------------------------------------


@router.get("/fixed-assets/depreciation-run", response_class=HTMLResponse, response_model=None)
async def depreciation_run_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the batch depreciation run form.

    Defaults the through_date to the last day of the current calendar month.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    _today = date.today()
    _last_day = calendar.monthrange(_today.year, _today.month)[1]
    through_default = date(_today.year, _today.month, _last_day).isoformat()

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/depreciation_run.html",
        {
            "through_default": through_default,
            "results": None,
            "run_summary": None,
            "errors": None,
            "error": None,
        },
    )


@router.post("/fixed-assets/depreciation-run", response_class=HTMLResponse, response_model=None)
async def depreciation_run_submit(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the batch depreciation run.

    Calls POST /api/v1/fixed_assets/depreciation_run_all with {"through": through}.
    On 200: renders results inline (no redirect).
    On 422 / other error: re-renders the form with an error banner.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    through = str(form_data.get("through", "")).strip()

    _today = date.today()
    _last_day = calendar.monthrange(_today.year, _today.month)[1]
    through_default = through or date(_today.year, _today.month, _last_day).isoformat()

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/fixed_assets/depreciation_run_all",
            json={"through": through},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        body = resp.json()
        run_summary = {
            "through": body.get("through", through),
            "total_assets": body.get("total_assets", 0),
            "total_amount": body.get("total_amount", "0.00"),
        }
        return _TEMPLATES.TemplateResponse(
            request,
            "fixed_assets/depreciation_run.html",
            {
                "through_default": through_default,
                "results": body.get("results", []),
                "run_summary": run_summary,
                "errors": body.get("errors", []),
                "error": None,
            },
        )

    # 422 or other error — extract a human-readable message.
    error_msg = f"API error: HTTP {resp.status_code}"
    try:
        detail = resp.json().get("detail", "")
        if isinstance(detail, str) and detail:
            error_msg = detail
        elif isinstance(detail, list) and detail:
            first = detail[0]
            error_msg = first.get("msg", error_msg) if isinstance(first, dict) else str(first)
    except Exception:
        pass

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/depreciation_run.html",
        {
            "through_default": through_default,
            "results": None,
            "run_summary": None,
            "errors": None,
            "error": error_msg,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/fixed-assets", response_class=HTMLResponse, response_model=None)
async def fixed_assets_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the fixed assets list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``fixed_assets/_table.html`` partial only.  Otherwise the full page
    (``fixed_assets/list.html``) is returned.
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
    assets: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/fixed_assets", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            assets = payload.get("items", [])
            total = payload.get("total", len(assets))
            # Client-side search filter (name/code) — the API has no free-text
            # search param so we filter locally on the returned page.
            if search:
                q = search.lower()
                assets = [
                    a for a in assets
                    if q in (a.get("name") or "").lower()
                    or q in (a.get("code") or "").lower()
                ]
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "assets": assets,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_status": status or "",
        "filter_search": search or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "fixed_assets/_table.html" if is_htmx else "fixed_assets/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/fixed-assets/{asset_id}", response_class=HTMLResponse, response_model=None)
async def fixed_asset_detail(
    request: Request,
    asset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single fixed asset detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/fixed_assets/{asset_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "fixed_assets/detail.html",
                {"asset": None, "error": "Fixed asset not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "fixed_assets/detail.html",
                {"asset": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    asset = resp.json()
    flash = request.session.pop("flash", None)

    # Compute default "through" date for the post-depreciation form:
    # last day of the current calendar month.
    _today = date.today()
    _last_day = calendar.monthrange(_today.year, _today.month)[1]
    through_default = date(_today.year, _today.month, _last_day).isoformat()

    # Fetch accounts for the convert-to-inventory dropdowns (only needed
    # on ACTIVE assets, but cheap enough to always fetch).
    accounts: list[dict] = []
    if asset.get("status") == "ACTIVE":
        accounts = await _fetch_accounts(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "fixed_assets/detail.html",
        {
            "asset": asset,
            "error": None,
            "flash": flash,
            "through_default": through_default,
            "accounts": accounts,
            "today": _today.isoformat(),
        },
    )
