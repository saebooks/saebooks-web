"""Transfers list, detail, and create views — account-to-account money movement.

GET  /transfers               — list page (paginated, HTMX-aware)
GET  /transfers/new           — empty create form
POST /transfers/new           — submit to upstream API; redirect on success,
                                re-render with errors on 400
GET  /transfers/{id}          — transfer detail
POST /transfers/{id}/reverse  — reverse a POSTED transfer

This is the sanctioned no-manual-JE path for moving money between two of a
company's own balance-sheet accounts (bank -> credit-card paydown, bank ->
director-loan repayment, bank/loan transfer). See engine
``saebooks/api/v1/transfers.py``.

Divergences from the credit_notes/expenses pattern:
- The engine has NO draft/post lifecycle — ``POST /transfers`` creates AND
  posts in one call (``create_and_post_transfer``). There is no edit route
  and no If-Match / optimistic locking on this record type at all.
- ``TransferListOut`` is ``{"items": [...]}`` — no ``total`` field, so list
  pagination is "does this page look full" rather than a true page count.
- Both legs must be balance-sheet accounts (ASSET / LIABILITY / EQUITY) of
  the active company — never a P&L account, never a header account, never
  the same account twice. The engine enforces this; the form just narrows
  the dropdown to the right account types.
- Business-rule failures come back as HTTP 400 with
  ``{"detail": {"code": "transfer_invalid", "detail": "<message>"}}`` (a
  nested dict, not the flat pydantic 422 shape credit_notes/expenses parse).
- Only two actions exist: create and reverse. No void, no archive, no
  hard-delete (the engine exposes none of those for transfers).

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import uuid
from datetime import date
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


def _parse_error_detail(resp) -> str:
    """Extract a human-readable message from the engine's error shapes.

    Transfers' business-rule failures nest the message under
    ``detail.detail`` (``{"code": "transfer_invalid", "detail": "..."}"});
    fall back gracefully for any other shape.
    """
    try:
        body = resp.json()
    except Exception:
        return f"API error: HTTP {resp.status_code}"
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("detail") or detail.get("code") or detail)
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            return str(first.get("msg", first))
        return str(first)
    if isinstance(detail, str):
        return detail
    return f"API error: HTTP {resp.status_code}"


async def _fetch_balance_sheet_accounts(client) -> list[dict]:
    """Return ASSET + LIABILITY + EQUITY accounts, sorted by code.

    Both legs of a transfer must be a balance-sheet account of the active
    company — mirrors the engine's ``_BALANCE_SHEET_TYPES`` allow-list in
    ``services/transfers.py``.
    """
    accounts: list[dict] = []
    for account_type in ("ASSET", "LIABILITY", "EQUITY"):
        resp = await client.get(
            "/api/v1/accounts",
            params={"account_type": account_type, "limit": 500, "offset": 0},
        )
        if resp.is_success:
            accounts.extend(resp.json().get("items", []))
    accounts.sort(key=lambda a: a.get("code", ""))
    return accounts


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/transfers", response_class=HTMLResponse, response_model=None)
async def transfers_list(
    request: Request,
    account_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the transfers list page (full or HTMX fragment).

    ``TransferListOut`` carries no ``total`` — pagination is "next page
    exists if this page came back full", not a true page count.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if account_id:
        params["account_id"] = account_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    transfers: list[dict] = []
    accounts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/transfers", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            transfers = resp.json().get("items", [])
        else:
            error = _parse_error_detail(resp)

        for acct in await _fetch_balance_sheet_accounts(client):
            accounts_by_id[acct["id"]] = acct

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    # No total to compare against — assume more exist if this page is full.
    next_offset = offset + limit if len(transfers) == limit else None

    flash = request.session.pop("flash", None)

    ctx = {
        "transfers": transfers,
        "accounts_by_id": accounts_by_id,
        "error": error,
        "flash": flash,
        "filter_account_id": account_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "transfers/_table.html" if is_htmx else "transfers/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /transfers/{transfer_id} so FastAPI
# resolves the literal path first.
# ---------------------------------------------------------------------------


@router.get("/transfers/new", response_class=HTMLResponse, response_model=None)
async def transfer_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-transfer form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        accounts = await _fetch_balance_sheet_accounts(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "transfers/new.html",
        {
            "form": {"transfer_date": today},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
        },
    )


@router.post("/transfers/new", response_class=HTMLResponse, response_model=None)
async def transfer_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-transfer form.

    Calls POST /api/v1/transfers — creates AND posts in one call (no draft
    stage). 201 -> 303 redirect to /transfers/{id}. 400 (business-rule
    rejection, e.g. P&L account, same account both legs) -> re-render with
    the engine's message. 422 (pydantic validation, e.g. malformed UUID) ->
    re-render with per-field errors.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in ("from_account_id", "to_account_id", "transfer_date", "description", "reference"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val
    amount = form.get("amount", "").strip()
    if amount:
        payload["amount"] = amount

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/transfers",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        request.session["flash"] = "Transfer recorded and posted."
        return RedirectResponse(url=f"/transfers/{created['id']}", status_code=303)

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
        errors["__all__"] = _parse_error_detail(resp)

    async with api_client(request) as client:
        accounts = await _fetch_balance_sheet_accounts(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "transfers/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "accounts": accounts,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Reverse — POST /{transfer_id}/reverse
# NOTE: MUST appear before the catch-all /{transfer_id} GET.
# ---------------------------------------------------------------------------


@router.post("/transfers/{transfer_id}/reverse", response_class=HTMLResponse, response_model=None)
async def transfer_reverse(request: Request, transfer_id: str) -> RedirectResponse:
    """Reverse a POSTED transfer.

    POSTs to POST /api/v1/transfers/{id}/reverse — no If-Match (the engine
    doesn't version this record type).
    - 200 -> 303 to detail with flash "Transfer reversed."
    - 409 -> already reversed / not reversible -> flash the engine message
    - 404 -> flash "Transfer not found."
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/transfers/{transfer_id}/reverse", json={})

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Transfer reversed."
    elif resp.status_code == 404:
        request.session["flash"] = "Transfer not found."
        return RedirectResponse(url="/transfers", status_code=303)
    else:
        request.session["flash"] = _parse_error_detail(resp)

    return RedirectResponse(url=f"/transfers/{transfer_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/transfers/{transfer_id}", response_class=HTMLResponse, response_model=None)
async def transfer_detail(request: Request, transfer_id: str) -> HTMLResponse | RedirectResponse:
    """Render a single transfer detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/transfers/{transfer_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "transfers/detail.html",
                {"transfer": None, "error": "Transfer not found", "flash": None,
                 "from_account": None, "to_account": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "transfers/detail.html",
                {"transfer": None, "error": _parse_error_detail(resp), "flash": None,
                 "from_account": None, "to_account": None},
                status_code=resp.status_code,
            )

        transfer = resp.json()

        from_account = None
        to_account = None
        fa_resp = await client.get(f"/api/v1/accounts/{transfer['from_account_id']}")
        if fa_resp.is_success:
            from_account = fa_resp.json()
        ta_resp = await client.get(f"/api/v1/accounts/{transfer['to_account_id']}")
        if ta_resp.is_success:
            to_account = ta_resp.json()

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "transfers/detail.html",
        {
            "transfer": transfer,
            "error": None,
            "flash": flash,
            "from_account": from_account,
            "to_account": to_account,
        },
    )
