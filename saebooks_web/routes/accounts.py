"""Accounts (Chart of Accounts) list, detail, create, edit, archive — Lane D cycles 9 + 23.

GET  /accounts              — list page (HTMX-aware, limit/offset pagination)
GET  /accounts/new          — empty create form; generates idempotency key
POST /accounts/new          — submit to upstream API; redirect on success,
                              re-render with errors on 422
GET  /accounts/{id}         — account detail (flash from session)
GET  /accounts/{id}/edit    — pre-populated edit form (version in hidden input)
                              If account is archived -> 422 + edit_blocked.html
POST /accounts/{id}/edit    — submit PATCH to API with If-Match; redirect on
                              success, re-render on 409 (conflict) or 422
POST /accounts/{id}/archive — soft-archive via archive_entity helper

Route ordering: /new + /{id}/edit + /{id}/archive MUST appear before the
catch-all /{id} GET so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.
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

# All 8 AccountType enum values — used in form templates.
ACCOUNT_TYPES = [
    ("ASSET", "Asset"),
    ("LIABILITY", "Liability"),
    ("EQUITY", "Equity"),
    ("INCOME", "Income"),
    ("OTHER_INCOME", "Other Income"),
    ("EXPENSE", "Expense"),
    ("COST_OF_SALES", "Cost of Sales"),
    ("OTHER_EXPENSE", "Other Expense"),
]


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


_ACCOUNT_TABS: dict[str, tuple[str, ...]] = {
    "all":       (),
    "asset":     ("ASSET",),
    "liability": ("LIABILITY",),
    "equity":    ("EQUITY",),
    "income":    ("INCOME", "OTHER_INCOME"),
    "expense":   ("EXPENSE", "COST_OF_SALES", "OTHER_EXPENSE"),
}


@router.get("/accounts", response_class=HTMLResponse, response_model=None)
async def accounts_list(
    request: Request,
    tab: str = "all",
    account_type: str | None = None,  # legacy ?account_type=… still respected
) -> HTMLResponse | RedirectResponse:
    """Render the accounts page as a collapsible tree by parent_id.

    The CoA is rarely deep enough that pagination helps — we'd rather
    show the whole hierarchy and let HTML <details> collapse what the
    user isn't looking at. Fetches all accounts in one call (API caps
    at 1000) and groups by parent_id server-side.

    ``tab`` controls the top-level account-type filter. Tabs collapse
    related types (e.g. expense covers EXPENSE / COST_OF_SALES /
    OTHER_EXPENSE) — see ``_ACCOUNT_TABS``. The legacy
    ``?account_type=…`` query param still works for direct API/links.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # Normalise tab → set of account_types.
    tab = (tab or "all").lower()
    if tab not in _ACCOUNT_TABS:
        tab = "all"
    tab_types: set[str] = set(_ACCOUNT_TABS[tab])

    # Always fetch all accounts (with balances). Filter in Python so
    # tabs and tree composition stay coherent.
    params: dict[str, object] = {
        "limit": 1000,
        "offset": 0,
        "include_balance": "true",
    }

    error: str | None = None
    accounts: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/accounts", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            accounts = payload.get("items", [])
            total = payload.get("total", len(accounts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Tab-side filter: by default keep everything; otherwise restrict to
    # the tab's account_types. Legacy ?account_type=… further narrows.
    filtered = accounts
    if tab_types:
        filtered = [a for a in filtered if a.get("account_type") in tab_types]
    if account_type:
        filtered = [a for a in filtered if a.get("account_type") == account_type]

    # ── Build a tree from the filtered subset ──────────────────────────
    by_id = {str(a["id"]): a for a in filtered}
    children_by_parent: dict[str | None, list[dict]] = {}
    for a in filtered:
        pid = a.get("parent_id")
        key: str | None = str(pid) if pid and str(pid) in by_id else None
        children_by_parent.setdefault(key, []).append(a)
    for k in children_by_parent:
        children_by_parent[k].sort(key=lambda a: (a.get("code") or ""))
    roots = children_by_parent.get(None, [])

    # ── Stamp subtree_balance on every node (header roll-up) ───────────
    from decimal import Decimal as _D

    def _subtree(nid: str) -> _D:
        node = by_id.get(nid, {})
        own = _D(node.get("balance") or "0") if not node.get("is_header") else _D("0")
        for child in children_by_parent.get(nid, []):
            own += _subtree(str(child["id"]))
        node["subtree_balance"] = str(own)
        return own

    for r in roots:
        _subtree(str(r["id"]))

    # Tab counts so the UI can show "Asset (32)" etc. without a second
    # fetch. Counted from the unfiltered fetch.
    tab_counts: dict[str, int] = {"all": len(accounts)}
    for tname, types in _ACCOUNT_TABS.items():
        if not types:
            continue
        tab_counts[tname] = sum(1 for a in accounts if a.get("account_type") in types)

    flash = request.session.pop("flash", None)

    ctx = {
        "accounts": filtered,
        "roots": roots,
        "children_by_parent": children_by_parent,
        "total": len(filtered),
        "grand_total": total,
        "tab": tab,
        "tab_counts": tab_counts,
        "filter_account_type": account_type or "",
        "error": error,
        "flash": flash,
    }

    return _TEMPLATES.TemplateResponse(request, "accounts/list.html", ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{account_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/accounts/new", response_class=HTMLResponse, response_model=None)
async def account_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-account form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "accounts/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "account_types": ACCOUNT_TYPES,
        },
    )


@router.post("/accounts/new", response_class=HTMLResponse, response_model=None)
async def account_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-account form.

    - 201 -> 303 redirect to /accounts/{id}
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

    for required_field in ("code", "name", "account_type"):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    for optional_field in ("description",):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    # parent_id is a UUID text input — only include when non-empty.
    parent_id = form.get("parent_id", "").strip()
    if parent_id:
        payload["parent_id"] = parent_id

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/accounts",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/accounts/{created['id']}", status_code=303)

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
        "accounts/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "account_types": ACCOUNT_TYPES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before /{account_id} catch-all.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "code",
    "name",
    "account_type",
    "description",
)


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse, response_model=None)
async def account_edit_form(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing account.

    If the account is already archived, renders edit_blocked.html with HTTP 422.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/accounts/{account_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "accounts/edit.html",
            {
                "account": None,
                "form": {},
                "errors": {"__all__": "Account not found"},
                "conflict": False,
                "account_types": ACCOUNT_TYPES,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "accounts/edit.html",
            {
                "account": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "account_types": ACCOUNT_TYPES,
            },
            status_code=resp.status_code,
        )

    account = resp.json()

    # Block editing archived accounts.
    if account.get("archived_at"):
        return _TEMPLATES.TemplateResponse(
            request,
            "accounts/edit_blocked.html",
            {"account": account},
            status_code=422,
        )

    form: dict[str, str] = {field: str(account.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(account.get("version", ""))

    return _TEMPLATES.TemplateResponse(
        request,
        "accounts/edit.html",
        {
            "account": account,
            "form": form,
            "errors": {},
            "conflict": False,
            "account_types": ACCOUNT_TYPES,
        },
    )


@router.post("/accounts/{account_id}/edit", response_class=HTMLResponse, response_model=None)
async def account_update(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /accounts/{id}
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
            f"/api/v1/accounts/{account_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Account saved."
        return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/accounts/{account_id}")

        server_account: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_account.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "accounts/edit.html",
            {
                "account": server_account,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_account": server_account,
                "account_types": ACCOUNT_TYPES,
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
            "PATCH /api/v1/accounts/%s returned 428 — If-Match header was missing",
            account_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "accounts/edit.html",
        {
            "account": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "account_types": ACCOUNT_TYPES,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{account_id}/archive
# NOTE: MUST appear before the catch-all /{account_id} GET.
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/archive", response_class=HTMLResponse, response_model=None)
async def account_archive(
    request: Request,
    account_id: str,
) -> RedirectResponse:
    """Soft-archive an account via DELETE /api/v1/accounts/{id} with If-Match.

    On success redirects to /accounts with a flash.
    On 409 (version conflict) or 422 (account in use) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/accounts",
        entity_id=account_id,
        version=str(version),
        entity_label=f"Account {account_id}",
        list_url="/accounts",
        detail_url=f"/accounts/{account_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/accounts/{account_id}", response_class=HTMLResponse, response_model=None)
async def account_detail(
    request: Request,
    account_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "date",
    direction: str = "desc",
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render a single account detail page with its GL ledger at the bottom.

    Same UX pattern as /bank-accounts/{id}: the page's bottom real
    estate hosts the transactions list (posted journal lines with
    running balance), with date filter + pagination inline.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000
    if offset < 0:
        offset = 0

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/accounts/{account_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "accounts/detail.html",
                {"account": None, "error": "Account not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "accounts/detail.html",
                {"account": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

        # Fetch the per-account ledger lines. Empty array is fine for
        # accounts with no posted activity (system-managed buckets etc.).
        lines: list[dict] = []
        lines_total = 0
        opening_balance = "0"
        total_debit = "0"
        total_credit = "0"
        credit_normal = False
        ledger_error: str | None = None
        if direction not in ("asc", "desc"):
            direction = "desc"
        ledger_params: dict[str, object] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "direction": direction,
        }
        if date_from:
            ledger_params["date_from"] = date_from
        if date_to:
            ledger_params["date_to"] = date_to
        ledger_resp = await client.get(
            f"/api/v1/accounts/{account_id}/ledger", params=ledger_params
        )
        if ledger_resp.is_success:
            body = ledger_resp.json()
            lines = body.get("items", [])
            lines_total = body.get("total", len(lines))
            opening_balance = body.get("opening_balance", "0")
            total_debit = body.get("total_debit", "0")
            total_credit = body.get("total_credit", "0")
            credit_normal = body.get("credit_normal", False)
        else:
            ledger_error = f"Could not load ledger: HTTP {ledger_resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < lines_total else None

    account = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "accounts/detail.html",
        {
            "account": account,
            "error": None,
            "flash": flash,
            "lines": lines,
            "lines_total": lines_total,
            "ledger_error": ledger_error,
            "opening_balance": opening_balance,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "credit_normal": credit_normal,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "sort": sort,
            "direction": direction,
            "limit": limit,
            "offset": offset,
            "prev_offset": prev_offset,
            "next_offset": next_offset,
        },
    )
