"""Supplier credit notes list, detail, and create views — purchase-side
mirror of credit_notes.py.

A supplier (purchase) credit note reverses a purchase: Dr AP control, Cr
expense, Cr GST Paid (input credit reversed). See engine
``saebooks/api/v1/supplier_credit_notes.py``.

GET  /supplier-credit-notes               — list page (paginated, HTMX-aware)
GET  /supplier-credit-notes/new           — empty create form
POST /supplier-credit-notes/new           — submit to upstream API
GET  /supplier-credit-notes/_add_line     — HTMX partial: blank line row
POST /supplier-credit-notes/{id}/post     — DRAFT -> POSTED
POST /supplier-credit-notes/{id}/void     — POSTED -> VOIDED
GET  /supplier-credit-notes/{id}          — detail

Route ordering: /supplier-credit-notes/new and /_add_line MUST be declared
before /supplier-credit-notes/{id}/post and /{id}/void, which must be
declared before /supplier-credit-notes/{id}.

A parallel page rather than a Sales/Purchases split on the existing
/credit-notes route — the existing UI has no notion of a purchases mode and
the two record types diverge (original_bill_id vs original_invoice_id,
supplier_reference vs no reference field, a SUPPLIER contact pool instead
of CUSTOMER). URL is hyphenated (/supplier-credit-notes); the API path uses
underscores (/api/v1/supplier_credit_notes) — same convention as
/credit-notes -> /api/v1/credit_notes.

Divergences from the credit_notes pattern:
- contact pool is SUPPLIER + BOTH (not CUSTOMER + BOTH) — contact_id is
  required (not optional, unlike receipts).
- "Applied to" links to a BILL (original_bill_id), not an invoice.
- supplier_reference (their credit note number) replaces the absent
  reference field on the sales side.
- Line items carry discount_pct (like invoices/bills) in addition to
  quantity/unit_price/tax_code_id — credit_notes' lines don't expose it in
  the UI, SCN does since the engine schema (SCNLineIn) includes it.
- Void returns 200 with the updated record (not 204).
- No archive/hard-delete route exists on the engine for this record type.

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


def _parse_business_error(resp) -> str:
    """Extract a human-readable message from the engine's error shapes."""
    try:
        body = resp.json()
    except Exception:
        return f"API error: HTTP {resp.status_code}"
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("detail") or detail.get("code") or detail)
    if isinstance(detail, list) and detail:
        first = detail[0]
        return str(first.get("msg", first)) if isinstance(first, dict) else str(first)
    if isinstance(detail, str):
        return detail
    return f"API error: HTTP {resp.status_code}"


async def _fetch_dropdowns(client) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch supplier contacts, accounts and tax_codes."""
    contacts: list[dict] = []
    for _ctype in ("SUPPLIER", "BOTH"):
        _r = await client.get(
            "/api/v1/contacts",
            params={"type": _ctype, "limit": 200, "offset": 0},
        )
        if _r.is_success:
            contacts.extend(_r.json().get("items", []))

    accounts: list[dict] = []
    a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    tax_codes: list[dict] = []
    t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    return contacts, accounts, tax_codes


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/supplier-credit-notes", response_class=HTMLResponse, response_model=None)
async def supplier_credit_notes_list(
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
    scns: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/supplier_credit_notes", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            scns = payload.get("items", [])
            total = payload.get("total", len(scns))
        else:
            error = _parse_business_error(resp)

        for ctype in ("SUPPLIER", "BOTH"):
            c_resp = await client.get(
                "/api/v1/contacts",
                params={"type": ctype, "limit": 500, "offset": 0},
            )
            if c_resp.is_success:
                for c in c_resp.json().get("items", []):
                    contacts_by_id[c["id"]] = c

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None
    flash = request.session.pop("flash", None)

    ctx = {
        "supplier_credit_notes": scns,
        "total": total,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
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
    template = "supplier_credit_notes/_table.html" if is_htmx else "supplier_credit_notes/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /supplier-credit-notes/{scn_id}.
# ---------------------------------------------------------------------------


@router.get("/supplier-credit-notes/new", response_class=HTMLResponse, response_model=None)
async def supplier_credit_note_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_dropdowns(client)

    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "supplier_credit_notes/new.html",
        {
            "form": {"issue_date": today},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/supplier-credit-notes/new", response_class=HTMLResponse, response_model=None)
async def supplier_credit_note_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in ("contact_id", "issue_date", "original_bill_id", "supplier_reference", "reason", "notes"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/supplier_credit_notes",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/supplier-credit-notes/{created['id']}", status_code=303)

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
        errors["__all__"] = _parse_business_error(resp)

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_dropdowns(client)

    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "supplier_credit_notes/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get("/supplier-credit-notes/_add_line", response_class=HTMLResponse, response_model=None)
async def supplier_credit_note_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        _, accounts, tax_codes = await _fetch_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "supplier_credit_notes/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "tax_codes": tax_codes,
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Post transition — POST /{scn_id}/post
# NOTE: MUST appear before the catch-all /{scn_id} GET.
# ---------------------------------------------------------------------------


@router.post("/supplier-credit-notes/{scn_id}/post", response_class=HTMLResponse, response_model=None)
async def supplier_credit_note_post(request: Request, scn_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/supplier_credit_notes/{scn_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": str(uuid.uuid4()),
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Supplier credit note posted."
        return RedirectResponse(url=f"/supplier-credit-notes/{scn_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/supplier-credit-notes/{scn_id}", status_code=303)

    request.session["flash"] = _parse_business_error(resp)
    return RedirectResponse(url=f"/supplier-credit-notes/{scn_id}", status_code=303)


# ---------------------------------------------------------------------------
# Void transition — POST /{scn_id}/void
# NOTE: MUST appear before the catch-all /{scn_id} GET. Void returns 200
# with the updated record (NOT 204, unlike credit_notes' void).
# ---------------------------------------------------------------------------


@router.post("/supplier-credit-notes/{scn_id}/void", response_class=HTMLResponse, response_model=None)
async def supplier_credit_note_void(request: Request, scn_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/supplier_credit_notes/{scn_id}/void",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Supplier credit note voided."
        return RedirectResponse(url=f"/supplier-credit-notes/{scn_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/supplier-credit-notes/{scn_id}", status_code=303)

    request.session["flash"] = _parse_business_error(resp)
    return RedirectResponse(url=f"/supplier-credit-notes/{scn_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/supplier-credit-notes/{scn_id}", response_class=HTMLResponse, response_model=None)
async def supplier_credit_note_detail(request: Request, scn_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/supplier_credit_notes/{scn_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "supplier_credit_notes/detail.html",
                {"scn": None, "error": "Supplier credit note not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "supplier_credit_notes/detail.html",
                {"scn": None, "error": _parse_business_error(resp), "flash": None},
                status_code=resp.status_code,
            )

    scn = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "supplier_credit_notes/detail.html",
        {"scn": scn, "error": None, "flash": flash},
    )
