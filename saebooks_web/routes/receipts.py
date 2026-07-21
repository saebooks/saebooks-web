"""Receipts list, detail, and create views — generic money-in record.

A receipt is the sanctioned record type for money arriving that is NOT an
invoice payment: supplier refunds, cashbacks, rebates, interest, an ATO GST
refund, an insurance recovery not tied to a bill. Dr bank/asset, Cr
income|expense (+ GST if applicable). See engine
``saebooks/api/v1/receipts.py``.

GET  /receipts               — list page (paginated, HTMX-aware)
GET  /receipts/new           — empty create form; generates idempotency key
POST /receipts/new           — submit to upstream API; redirect on success,
                                re-render with errors on 422
GET  /receipts/_add_line     — HTMX partial: returns a single blank line row
POST /receipts/{id}/post     — transition DRAFT -> POSTED
POST /receipts/{id}/void     — transition POSTED -> VOIDED
GET  /receipts/{id}          — receipt detail

Route ordering: /receipts/new and /receipts/_add_line MUST be declared
before /receipts/{receipt_id}/post and /receipts/{receipt_id}/void, which
must be declared before /receipts/{receipt_id}, so FastAPI resolves the
literal paths first.

Divergences from the credit_notes pattern (mirrors expenses' shape instead,
per the engine's line schema):
- ``ReceiptLineIn`` is ``{description, account_id, tax_code_id, amount}`` —
  a flat amount per line, NOT quantity/unit_price. The line-row template
  and ``form_helpers.parse_lines`` (extended with "amount") reflect that.
- ``bank_account_id`` (required) sources from ``/api/v1/bank_accounts``
  (the bank-side view over accounts), same as payments.py — not a generic
  ASSET account_type filter.
- ``contact_id`` is optional and supplier-side (SUPPLIER + BOTH pool) — a
  refund often has no counterparty on file (e.g. an ATO GST refund).
- Void returns 200 with the updated record (not 204 like credit_notes).
- No edit route and no archive/hard-delete — the engine exposes none for
  receipts; scope matches the expenses UI (list, create, post, void).

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
from saebooks_web.form_helpers import parse_lines as _parse_lines

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return (contacts, bank_accounts, income/expense accounts, tax_codes).

    contacts pulls SUPPLIER + BOTH — a receipt's counterparty (if any) is
    supplier-side (the party refunding money to us), mirroring expenses.py.
    """
    contacts: list[dict] = []
    for _ctype in ("SUPPLIER", "BOTH"):
        _r = await client.get(
            "/api/v1/contacts",
            params={"type": _ctype, "limit": 500, "offset": 0},
        )
        if _r.is_success:
            contacts.extend(_r.json().get("items", []))

    bank_accounts: list[dict] = []
    ba_resp = await client.get("/api/v1/bank_accounts", params={"limit": 200, "offset": 0})
    if ba_resp.is_success:
        bank_accounts = ba_resp.json().get("items", [])

    # Lines can be coded to income (a rebate that offsets nothing) or
    # expense (a supplier refund crediting back the original spend) —
    # pull both pools, like credit_notes pulls all accounts.
    accounts: list[dict] = []
    a_resp = await client.get("/api/v1/accounts", params={"limit": 500, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    tax_codes: list[dict] = []
    t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    return contacts, bank_accounts, accounts, tax_codes


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/receipts", response_class=HTMLResponse, response_model=None)
async def receipts_list(
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
    receipts: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}
    bank_accounts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/receipts", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            receipts = payload.get("items", [])
            total = payload.get("total", len(receipts))
        else:
            error = f"API error: HTTP {resp.status_code}"

        for ctype in ("SUPPLIER", "BOTH"):
            c_resp = await client.get(
                "/api/v1/contacts",
                params={"type": ctype, "limit": 500, "offset": 0},
            )
            if c_resp.is_success:
                for c in c_resp.json().get("items", []):
                    contacts_by_id[c["id"]] = c

        ba_resp = await client.get("/api/v1/bank_accounts", params={"limit": 200, "offset": 0})
        if ba_resp.is_success:
            for a in ba_resp.json().get("items", []):
                bank_accounts_by_id[a["id"]] = a

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None
    flash = request.session.pop("flash", None)

    ctx = {
        "receipts": receipts,
        "total": total,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
        "bank_accounts_by_id": bank_accounts_by_id,
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
    template = "receipts/_table.html" if is_htmx else "receipts/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /receipts/{receipt_id}.
# ---------------------------------------------------------------------------


@router.get("/receipts/new", response_class=HTMLResponse, response_model=None)
async def receipt_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        contacts, bank_accounts, accounts, tax_codes = await _fetch_dropdowns(client)

    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "receipts/new.html",
        {
            "form": {"receipt_date": today},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "bank_accounts": bank_accounts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/receipts/new", response_class=HTMLResponse, response_model=None)
async def receipt_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in ("bank_account_id", "receipt_date", "contact_id", "reference", "reason", "notes"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/receipts",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/receipts/{created['id']}", status_code=303)

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
        # 400 — the engine's ReceiptError shape nests detail under "detail".
        try:
            body = resp.json()
            detail = body.get("detail")
            if isinstance(detail, dict):
                errors["__all__"] = str(detail.get("detail") or detail.get("code") or detail)
            elif isinstance(detail, str):
                errors["__all__"] = detail
            else:
                errors["__all__"] = f"API error: HTTP {resp.status_code}"
        except Exception:
            errors["__all__"] = f"API error: HTTP {resp.status_code}"

    async with api_client(request) as client:
        contacts, bank_accounts, accounts, tax_codes = await _fetch_dropdowns(client)

    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "receipts/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "bank_accounts": bank_accounts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get("/receipts/_add_line", response_class=HTMLResponse, response_model=None)
async def receipt_add_line(request: Request, index: int = 0) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        _, _, accounts, tax_codes = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "receipts/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "tax_codes": tax_codes,
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Post transition — POST /{receipt_id}/post
# NOTE: MUST appear before the catch-all /{receipt_id} GET.
# ---------------------------------------------------------------------------


@router.post("/receipts/{receipt_id}/post", response_class=HTMLResponse, response_model=None)
async def receipt_post(request: Request, receipt_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/receipts/{receipt_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": str(uuid.uuid4()),
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Receipt posted."
        return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)

    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("code") or str(detail)
        elif isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)


# ---------------------------------------------------------------------------
# Void transition — POST /{receipt_id}/void
# NOTE: MUST appear before the catch-all /{receipt_id} GET.
# Void returns 200 with the updated record (NOT 204 — differs from
# credit_notes' void, which returns 204 No Content).
# ---------------------------------------------------------------------------


@router.post("/receipts/{receipt_id}/void", response_class=HTMLResponse, response_model=None)
async def receipt_void(request: Request, receipt_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/receipts/{receipt_id}/void",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Receipt voided."
        return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)

    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("code") or str(detail)
        elif isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/receipts/{receipt_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/receipts/{receipt_id}", response_class=HTMLResponse, response_model=None)
async def receipt_detail(request: Request, receipt_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/receipts/{receipt_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "receipts/detail.html",
                {"receipt": None, "error": "Receipt not found", "flash": None,
                 "contact_name": "", "bank_account_name": ""},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "receipts/detail.html",
                {"receipt": None, "error": f"API error: HTTP {resp.status_code}", "flash": None,
                 "contact_name": "", "bank_account_name": ""},
                status_code=resp.status_code,
            )

        receipt = resp.json()

        contact_name = ""
        if receipt.get("contact_id"):
            c = await client.get(f"/api/v1/contacts/{receipt['contact_id']}")
            if c.is_success:
                contact_name = c.json().get("name", "")

        bank_account_name = ""
        if receipt.get("bank_account_id"):
            a = await client.get(f"/api/v1/accounts/{receipt['bank_account_id']}")
            if a.is_success:
                ba = a.json()
                bank_account_name = f"{ba.get('code', '')} {ba.get('name', '')}".strip()

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "receipts/detail.html",
        {
            "receipt": receipt,
            "error": None,
            "flash": flash,
            "contact_name": contact_name,
            "bank_account_name": bank_account_name,
        },
    )
