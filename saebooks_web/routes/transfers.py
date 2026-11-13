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

import asyncio
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


# What a transfer is for (P1-50, batch 14) — mirrors the engine's
# TRANSFER_PURPOSES. ``to_types``/``from_types`` narrow the account lists in
# the form; the engine checks the same rules.
PURPOSES: list[dict[str, object]] = [
    {
        "value": "own_accounts",
        "label": "Between my own accounts",
        "help": "Moving money from one business bank account to another, or into savings.",
        "from_types": ["ASSET"],
        "to_types": ["ASSET"],
    },
    {
        "value": "card_payment",
        "label": "Paying off a credit card",
        "help": "Paying the business credit card from the bank. The card account goes down by the amount paid. Do not record this as an expense: the purchases on the card were the expenses.",
        "from_types": ["ASSET"],
        "to_types": ["LIABILITY"],
    },
    {
        "value": "drawings",
        "label": "Owner taking money out",
        "help": "The owner taking money for themselves. This is not a business expense. For a company, use the director loan account.",
        "from_types": ["ASSET"],
        "to_types": ["EQUITY", "LIABILITY"],
    },
    {
        "value": "owner_contribution",
        "label": "Owner putting money in",
        "help": "The owner paying their own money into the business, as capital or as a loan to the business.",
        "from_types": ["EQUITY", "LIABILITY"],
        "to_types": ["ASSET"],
    },
    {
        "value": "loan_repayment",
        "label": "Loan repayment without interest",
        "help": "Paying down a loan where no interest is charged. For a loan, chattel mortgage or hire purchase with interest, record the repayment under Loans and finance so the interest is split out.",
        "from_types": ["ASSET"],
        "to_types": ["LIABILITY"],
    },
    {
        "value": "owner_reimbursement",
        "label": "Paying the owner back",
        "help": "Repaying the owner for business costs they paid personally. To pay back specific expenses, use Owner money, Reimburse selected.",
        "from_types": ["ASSET"],
        "to_types": ["LIABILITY", "EQUITY"],
    },
]
_PURPOSE_VALUES = {p["value"] for p in PURPOSES}


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
async def transfer_new_form(
    request: Request,
    purpose: str | None = None,
    from_account_id: str | None = None,
    to_account_id: str | None = None,
    amount: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the create-transfer form.

    Query parameters pre-fill it, so a "Pay card" button on a credit card
    account can open ``/transfers/new?purpose=card_payment&to_account_id=…``.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        accounts = await _fetch_balance_sheet_accounts(client)

    form: dict[str, str] = {"transfer_date": today}
    if purpose in _PURPOSE_VALUES:
        form["purpose"] = purpose
    if from_account_id:
        form["from_account_id"] = from_account_id
    if to_account_id:
        form["to_account_id"] = to_account_id
    if amount:
        form["amount"] = amount

    return _TEMPLATES.TemplateResponse(
        request,
        "transfers/new.html",
        {
            "form": form,
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
            "purposes": PURPOSES,
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
    purpose = form.get("purpose", "").strip()
    if purpose in _PURPOSE_VALUES:
        payload["purpose"] = purpose

    # The idempotency key is minted once per form render, so a second click
    # (or a reload of the POST) replays the first transfer instead of posting
    # a second one. While the first is still posting the engine answers 503 —
    # wait briefly and ask again rather than showing an error.
    async with api_client(request) as client:
        for _attempt in range(4):
            resp = await client.post(
                "/api/v1/transfers",
                json=payload,
                headers={"X-Idempotency-Key": idempotency_key},
            )
            if resp.status_code != 503:
                break
            await asyncio.sleep(0.5)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        request.session["flash"] = "Transfer recorded and posted."
        return RedirectResponse(url=f"/transfers/{created['id']}", status_code=303)

    errors: dict[str, str] = {}
    body_code = None
    try:
        body_code = resp.json().get("code")
    except Exception:
        body_code = None
    if body_code == "idempotency_key_conflict":
        # The form was resubmitted with changed values after a transfer was
        # already recorded from it. Give the re-rendered form a fresh key.
        errors["__all__"] = (
            "A transfer was already recorded from this form. Check the transfers "
            "list before recording another; this form is now ready for a new one."
        )
        idempotency_key = str(uuid.uuid4())
    elif resp.status_code == 503:
        errors["__all__"] = "The transfer is still being recorded. Check the transfers list in a moment."
    elif resp.status_code == 422:
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
            "purposes": PURPOSES,
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
            "purpose_label": next(
                (p["label"] for p in PURPOSES if p["value"] == transfer.get("purpose")),
                None,
            ),
        },
    )
