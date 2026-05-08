"""Purchase-order list, detail, create, send, cancel, close + convert-to-bill.

GET  /purchase_orders                — list page (paginated, HTMX-aware)
GET  /purchase_orders/new            — empty create form
POST /purchase_orders/new            — submit to API
GET  /purchase_orders/_add_line      — HTMX partial: blank line row
GET  /purchase_orders/{id}           — detail view (with state-transition buttons)
POST /purchase_orders/{id}/send      — DRAFT → OPEN
POST /purchase_orders/{id}/cancel    — non-terminal → CANCELLED
POST /purchase_orders/{id}/close     — OPEN/PARTIAL/RECEIVED → CLOSED
POST /purchase_orders/{id}/convert   — convert-to-bill (default-full or partial)
POST /purchase_orders/{id}/archive   — soft-archive

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.form_helpers import parse_lines as _parse_lines

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    c_resp = await client.get(
        "/api/v1/contacts",
        params={"contact_type": "SUPPLIER", "limit": 500, "offset": 0},
    )
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])

    a_resp = await client.get(
        "/api/v1/accounts",
        params={"account_type": "EXPENSE", "limit": 500, "offset": 0},
    )
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    t_resp = await client.get(
        "/api/v1/tax_codes", params={"page_size": 500}
    )
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    p_resp = await client.get(
        "/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0}
    )
    if p_resp.is_success:
        projects = p_resp.json().get("items", [])

    return contacts, accounts, tax_codes, projects


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/purchase_orders", response_class=HTMLResponse, response_model=None)
async def purchase_orders_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if contact_id:
        params["contact_id"] = contact_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    pos: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/purchase_orders", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            pos = payload.get("items", [])
            total = payload.get("total", len(pos))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None
    flash = request.session.pop("flash", None)

    ctx = {
        "purchase_orders": pos,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_status": status or "",
        "filter_contact_id": contact_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = (
        "purchase_orders/_table.html"
        if is_htmx
        else "purchase_orders/list.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# ---------------------------------------------------------------------------


@router.get("/purchase_orders/new", response_class=HTMLResponse, response_model=None)
async def po_new_form(
    request: Request,
    contact_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()
    expected = (date.today() + timedelta(days=14)).isoformat()

    async with api_client(request) as client:
        contacts, accounts, tax_codes, projects = await _fetch_dropdowns(client)

    initial_lines = [{"index": 0}]

    form: dict[str, object] = {
        "issue_date": today,
        "expected_date": expected,
    }
    if contact_id:
        form["contact_id"] = contact_id

    return _TEMPLATES.TemplateResponse(
        request,
        "purchase_orders/new.html",
        {
            "form": form,
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/purchase_orders/new", response_class=HTMLResponse, response_model=None)
async def po_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in ("contact_id", "issue_date", "expected_date", "delivery_address", "notes"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    currency = form.get("currency", "").strip().upper() or "AUD"
    payload["currency"] = currency
    fx_rate_raw = form.get("fx_rate", "").strip()
    if fx_rate_raw and currency != "AUD":
        payload["fx_rate"] = fx_rate_raw

    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/purchase_orders",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(
            url=f"/purchase_orders/{created['id']}", status_code=303
        )

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field_key = str(field_parts[0]) if field_parts else "__all__"
                    errors[field_key] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    async with api_client(request) as client:
        contacts, accounts, tax_codes, projects = await _fetch_dropdowns(client)

    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "purchase_orders/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get(
    "/purchase_orders/_add_line", response_class=HTMLResponse, response_model=None
)
async def po_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        _, accounts, tax_codes, projects = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "purchase_orders/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/purchase_orders/{po_id}", response_class=HTMLResponse, response_model=None)
async def po_detail(
    request: Request, po_id: str
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/purchase_orders/{po_id}")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "purchase_orders/detail.html",
            {"po": None, "error": "Purchase order not found"},
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "purchase_orders/detail.html",
            {"po": None, "error": f"API error: HTTP {resp.status_code}"},
            status_code=resp.status_code,
        )

    po = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "purchase_orders/detail.html",
        {"po": po, "error": None, "flash": flash},
    )


# ---------------------------------------------------------------------------
# State transitions: send / cancel / close / archive
# ---------------------------------------------------------------------------


async def _state_transition(
    request: Request, po_id: str, action: str
) -> RedirectResponse:
    """POST /api/v1/purchase_orders/{id}/{action} with current version."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        # Fetch current version for If-Match
        get_resp = await client.get(f"/api/v1/purchase_orders/{po_id}")
        if not get_resp.is_success:
            request.session["flash"] = (
                f"Could not fetch PO before {action}: HTTP {get_resp.status_code}"
            )
            return RedirectResponse(
                url=f"/purchase_orders/{po_id}", status_code=303
            )
        version = get_resp.json().get("version", 1)

        resp = await client.post(
            f"/api/v1/purchase_orders/{po_id}/{action}",
            headers={"If-Match": str(version)},
        )

    if resp.status_code == 200:
        request.session["flash"] = f"Purchase order {action}ed."
    else:
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text
        request.session["flash"] = (
            f"{action.capitalize()} failed: HTTP {resp.status_code}: {detail}"
        )

    return RedirectResponse(url=f"/purchase_orders/{po_id}", status_code=303)


@router.post(
    "/purchase_orders/{po_id}/send", response_class=HTMLResponse, response_model=None
)
async def po_send(request: Request, po_id: str) -> RedirectResponse:
    return await _state_transition(request, po_id, "send")


@router.post(
    "/purchase_orders/{po_id}/cancel",
    response_class=HTMLResponse,
    response_model=None,
)
async def po_cancel(request: Request, po_id: str) -> RedirectResponse:
    return await _state_transition(request, po_id, "cancel")


@router.post(
    "/purchase_orders/{po_id}/close", response_class=HTMLResponse, response_model=None
)
async def po_close(request: Request, po_id: str) -> RedirectResponse:
    return await _state_transition(request, po_id, "close")


@router.post(
    "/purchase_orders/{po_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def po_archive(
    request: Request, po_id: str
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        get_resp = await client.get(f"/api/v1/purchase_orders/{po_id}")
        if not get_resp.is_success:
            request.session["flash"] = "PO not found"
            return RedirectResponse(url="/purchase_orders", status_code=303)
        version = get_resp.json().get("version", 1)

        resp = await client.delete(
            f"/api/v1/purchase_orders/{po_id}",
            headers={"If-Match": str(version)},
        )

    if resp.status_code == 204:
        request.session["flash"] = "Purchase order archived."
        return RedirectResponse(url="/purchase_orders", status_code=303)

    request.session["flash"] = (
        f"Archive failed: HTTP {resp.status_code}"
    )
    return RedirectResponse(url=f"/purchase_orders/{po_id}", status_code=303)


# ---------------------------------------------------------------------------
# Convert-to-bill
# ---------------------------------------------------------------------------


@router.post(
    "/purchase_orders/{po_id}/convert",
    response_class=HTMLResponse,
    response_model=None,
)
async def po_convert(
    request: Request, po_id: str
) -> RedirectResponse:
    """Convert (full or partial) to a DRAFT bill, then redirect to the bill."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    # Optional per-line quantities. Form keys: qty[<line_no>] = "5"
    quantities: dict[int, str] | None = None
    qty_keys = [k for k in form.keys() if k.startswith("qty[") and k.endswith("]")]
    if qty_keys:
        quantities = {}
        for k in qty_keys:
            try:
                line_no = int(k[4:-1])
            except ValueError:
                continue
            val = form.get(k, "").strip()
            if val:
                quantities[line_no] = val
        if not quantities:
            quantities = None

    payload: dict[str, object] = {}
    if quantities is not None:
        payload["quantities"] = quantities
    for field in ("bill_issue_date", "bill_due_date", "supplier_reference"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    async with api_client(request) as client:
        get_resp = await client.get(f"/api/v1/purchase_orders/{po_id}")
        if not get_resp.is_success:
            request.session["flash"] = "PO not found"
            return RedirectResponse(url="/purchase_orders", status_code=303)
        version = get_resp.json().get("version", 1)

        resp = await client.post(
            f"/api/v1/purchase_orders/{po_id}/convert-to-bill",
            json=payload,
            headers={"If-Match": str(version)},
        )

    if resp.status_code == 200:
        body = resp.json()
        bill_id = body.get("bill_id")
        request.session["flash"] = "Converted to draft bill."
        if bill_id:
            return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)
        return RedirectResponse(url=f"/purchase_orders/{po_id}", status_code=303)

    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text
    request.session["flash"] = (
        f"Convert failed: HTTP {resp.status_code}: {detail}"
    )
    return RedirectResponse(url=f"/purchase_orders/{po_id}", status_code=303)
