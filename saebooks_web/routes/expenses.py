"""Paid-at-checkout expense list / new / detail / post / void.

Sibling of bills.py but with no AP step — the expense's selected
payment account (bank / card / cash) is credited the moment the
expense posts. There is no separate Payment row to allocate against
later. Match the purchase_orders.py route shape for HTMX behaviour
and form parsing.

GET  /expenses                — list page (paginated, HTMX-aware)
GET  /expenses/new            — empty create form
POST /expenses/new            — submit to API
GET  /expenses/_add_line      — HTMX partial: blank line row
GET  /expenses/{id}           — detail view
POST /expenses/{id}/post      — DRAFT → POSTED with JE generation
POST /expenses/{id}/void      — non-VOIDED → VOIDED with JE reversal
POST /expenses/{id}/archive   — soft-archive
"""
from __future__ import annotations

import uuid
from datetime import date
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


async def _fetch_dropdowns(
    client,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Return (contacts, expense_accounts, payment_accounts, tax_codes, projects).

    payment_accounts is the bank/card/cash credit pool — ASSET + LIABILITY
    accounts. We fetch in two calls because the /accounts list filter takes
    a single account_type.
    """
    contacts: list[dict] = []
    expense_accounts: list[dict] = []
    payment_accounts: list[dict] = []
    tax_codes: list[dict] = []
    projects: list[dict] = []

    c_resp = await client.get(
        "/api/v1/contacts",
        params={"type": "SUPPLIER", "limit": 500, "offset": 0},
    )
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])

    e_resp = await client.get(
        "/api/v1/accounts",
        params={"account_type": "EXPENSE", "limit": 500, "offset": 0},
    )
    if e_resp.is_success:
        expense_accounts = e_resp.json().get("items", [])

    asset_resp = await client.get(
        "/api/v1/accounts",
        params={"account_type": "ASSET", "limit": 500, "offset": 0},
    )
    liab_resp = await client.get(
        "/api/v1/accounts",
        params={"account_type": "LIABILITY", "limit": 500, "offset": 0},
    )
    if asset_resp.is_success:
        payment_accounts.extend(asset_resp.json().get("items", []))
    if liab_resp.is_success:
        payment_accounts.extend(liab_resp.json().get("items", []))
    payment_accounts.sort(key=lambda a: a.get("code", ""))

    t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    p_resp = await client.get(
        "/api/v1/projects", params={"status": "ACTIVE", "limit": 200, "offset": 0}
    )
    if p_resp.is_success:
        projects = p_resp.json().get("items", [])

    return contacts, expense_accounts, payment_accounts, tax_codes, projects


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


_EXPENSE_SORT_KEYS = {"number", "expense_date", "reference", "total", "status"}


def _expense_sort_key(e: dict, key: str) -> object:
    if key == "total":
        try:
            return float(e.get("total") or 0)
        except (TypeError, ValueError):
            return 0.0
    return str(e.get(key) or "")


@router.get("/expenses", response_class=HTMLResponse, response_model=None)
async def expenses_list(
    request: Request,
    status: str | None = None,
    contact_id: str | None = None,
    payment_account_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "expense_date",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    sort = sort if sort in _EXPENSE_SORT_KEYS else "expense_date"
    direction = "asc" if direction == "asc" else "desc"

    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if contact_id:
        params["contact_id"] = contact_id
    if payment_account_id:
        params["payment_account_id"] = payment_account_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    expenses: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/expenses", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            expenses = payload.get("items", [])
            total = payload.get("total", len(expenses))
        else:
            error = f"API error: HTTP {resp.status_code}"

        # Resolve contact_id -> contact dict so the template can render the
        # supplier NAME (not the raw UUID). Mirrors bills.py — fetch suppliers
        # at page_size=500 which covers the largest CoAs we deal with; if an
        # expense row references a contact outside the supplier filter, the
        # template falls back to '—'.
        c_resp = await client.get(
            "/api/v1/contacts",
            params={"type": "SUPPLIER", "limit": 500, "offset": 0},
        )
        if c_resp.is_success:
            for c in c_resp.json().get("items", []):
                contacts_by_id[c["id"]] = c

    expenses.sort(key=lambda e: _expense_sort_key(e, sort), reverse=(direction == "desc"))

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None
    flash = request.session.pop("flash", None)

    ctx = {
        "expenses": expenses,
        "contacts_by_id": contacts_by_id,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_status": status or "",
        "filter_contact_id": contact_id or "",
        "filter_payment_account_id": payment_account_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "sort": sort,
        "direction": direction,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "expenses/_table.html" if is_htmx else "expenses/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# ---------------------------------------------------------------------------


@router.get("/expenses/new", response_class=HTMLResponse, response_model=None)
async def expense_new_form(
    request: Request,
    contact_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        contacts, expense_accounts, payment_accounts, tax_codes, projects = (
            await _fetch_dropdowns(client)
        )

    initial_lines = [{"index": 0}]

    form: dict[str, object] = {"expense_date": today}
    if contact_id:
        form["contact_id"] = contact_id

    return _TEMPLATES.TemplateResponse(
        request,
        "expenses/new.html",
        {
            "form": form,
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": expense_accounts,
            "payment_accounts": payment_accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/expenses/new", response_class=HTMLResponse, response_model=None)
async def expense_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in (
        "contact_id",
        "payment_account_id",
        "expense_date",
        "reference",
        "notes",
    ):
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
            "/api/v1/expenses",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        request.session["flash"] = f"Expense {created.get('reference') or created['id']} created."
        return RedirectResponse(url=f"/expenses/{created['id']}", status_code=303)

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
        contacts, expense_accounts, payment_accounts, tax_codes, projects = (
            await _fetch_dropdowns(client)
        )

    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "expenses/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "accounts": expense_accounts,
            "payment_accounts": payment_accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get("/expenses/_add_line", response_class=HTMLResponse, response_model=None)
async def expense_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        _, accounts, _, tax_codes, projects = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "expenses/_line_row.html",
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


@router.get("/expenses/{expense_id}", response_class=HTMLResponse, response_model=None)
async def expense_detail(
    request: Request, expense_id: str
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/expenses/{expense_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return HTMLResponse("Expense not found", status_code=404)

        expense = resp.json()

        # Look up display names for the FKs (best-effort).
        contact_name = ""
        if expense.get("contact_id"):
            c = await client.get(f"/api/v1/contacts/{expense['contact_id']}")
            if c.is_success:
                contact_name = c.json().get("name", "")

        payment_account_name = ""
        pa_id = expense.get("payment_account_id")
        if pa_id:
            a = await client.get(f"/api/v1/accounts/{pa_id}")
            if a.is_success:
                pa = a.json()
                payment_account_name = f"{pa.get('code', '')} {pa.get('name', '')}".strip()

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "expenses/detail.html",
        {
            "expense": expense,
            "contact_name": contact_name,
            "payment_account_name": payment_account_name,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# State transitions: post / void / archive
# ---------------------------------------------------------------------------


async def _transition(
    request: Request, expense_id: str, action: str
) -> RedirectResponse:
    """Helper: POST /api/v1/expenses/{id}/{action} with If-Match from current version."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        cur = await client.get(f"/api/v1/expenses/{expense_id}")
        if cur.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if cur.status_code == 404:
            request.session["flash"] = "Expense not found."
            return RedirectResponse(url="/expenses", status_code=303)

        version = cur.json()["version"]
        resp = await client.post(
            f"/api/v1/expenses/{expense_id}/{action}",
            headers={
                "If-Match": str(version),
                "X-Idempotency-Key": str(uuid.uuid4()),
            },
        )

    if resp.status_code == 200:
        request.session["flash"] = f"Expense {action}ed."
    elif resp.status_code == 409:
        request.session["flash"] = "Version conflict — refresh and try again."
    else:
        request.session["flash"] = f"API error: HTTP {resp.status_code}"

    return RedirectResponse(url=f"/expenses/{expense_id}", status_code=303)


@router.post("/expenses/{expense_id}/post", response_class=HTMLResponse, response_model=None)
async def expense_post(request: Request, expense_id: str) -> RedirectResponse:
    return await _transition(request, expense_id, "post")


@router.post("/expenses/{expense_id}/void", response_class=HTMLResponse, response_model=None)
async def expense_void(request: Request, expense_id: str) -> RedirectResponse:
    return await _transition(request, expense_id, "void")


@router.post(
    "/expenses/{expense_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def expense_archive(
    request: Request, expense_id: str
) -> RedirectResponse:
    """Soft-archive via DELETE on /api/v1/expenses/{id}."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        cur = await client.get(f"/api/v1/expenses/{expense_id}")
        if cur.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if cur.status_code == 404:
            request.session["flash"] = "Expense not found."
            return RedirectResponse(url="/expenses", status_code=303)
        version = cur.json()["version"]
        resp = await client.delete(
            f"/api/v1/expenses/{expense_id}",
            headers={"If-Match": str(version)},
        )

    if resp.status_code == 204:
        request.session["flash"] = "Expense archived."
        return RedirectResponse(url="/expenses", status_code=303)
    request.session["flash"] = f"Archive failed: HTTP {resp.status_code}"
    return RedirectResponse(url=f"/expenses/{expense_id}", status_code=303)


# ---------------------------------------------------------------------------
# Bulk action — POST /expenses/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_EXPENSES = {
    "post": ("POST", "/api/v1/expenses/{id}/post"),
    "void": ("POST", "/api/v1/expenses/{id}/void"),
    "archive": ("DELETE", "/api/v1/expenses/{id}"),
}


@router.post("/expenses/bulk", response_class=HTMLResponse, response_model=None)
async def expenses_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many expenses at once.

    Form fields:
      action  — one of: post, void, archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /expenses. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_EXPENSES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/expenses", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/expenses", status_code=303)

    method, path_tpl = _BULK_ACTIONS_EXPENSES[action]
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
        request.session["flash"] = f"{label}: {ok} expense{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/expenses", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/expenses/{expense_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def expense_hard_delete(request: Request, expense_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/expenses",
        entity_id=expense_id,
        entity_label=f"Expense {expense_id}",
        list_url="/expenses",
        detail_url=f"/expenses/{expense_id}",
    )
