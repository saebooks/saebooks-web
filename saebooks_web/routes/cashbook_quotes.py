"""Cashbook UI — phone-first quoting for sole traders.

Routes
------
GET  /cashbook/quotes                      — list
GET  /cashbook/quotes/new                  — simple one-line form
POST /cashbook/quotes/new                  — submit
GET  /cashbook/quotes/{id}                 — detail
POST /cashbook/quotes/{id}/send            — DRAFT → SENT
POST /cashbook/quotes/{id}/accept          — SENT → ACCEPTED
POST /cashbook/quotes/{id}/decline         — SENT → DECLINED
POST /cashbook/quotes/{id}/convert         — ACCEPTED → INVOICED (creates invoice)

Same simplifications as cashbook_invoices: free-text customer, single line,
GST toggle, Net 28 default expiry.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.routes.cashbook_invoices import (
    _first_income_account_id,
    _guard,
    _lookup_or_create_contact,
    _money,
    _tax_code_id,
)

logger = logging.getLogger("saebooks_web.cashbook_quotes")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# GET /cashbook/quotes
# ---------------------------------------------------------------------------


@router.get("/cashbook/quotes", response_class=HTMLResponse, response_model=None)
async def cashbook_quotes_list(request: Request) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    quotes: list[dict] = []
    contacts_by_id: dict[str, dict] = {}
    async with api_client(request) as client:
        resp = await client.get("/api/v1/quotes", params={"limit": 100, "offset": 0})
        if resp.is_success:
            quotes = resp.json().get("items", [])
        c_resp = await client.get(
            "/api/v1/contacts",
            params={"type": "CUSTOMER", "limit": 200, "offset": 0},
        )
        if c_resp.is_success:
            for c in c_resp.json().get("items", []):
                contacts_by_id[c["id"]] = c

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/quotes/list.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "quotes": quotes,
            "contacts_by_id": contacts_by_id,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# GET /cashbook/quotes/new
# ---------------------------------------------------------------------------


@router.get("/cashbook/quotes/new", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_new(request: Request) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    today = date.today().isoformat()
    expiry = (date.today() + timedelta(days=28)).isoformat()
    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/quotes/new.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "form": {
                "customer_name": "",
                "customer_email": "",
                "description": "Services",
                "amount": "",
                "gst": "on",
                "issue_date": today,
                "expiry_date": expiry,
            },
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
        },
    )


# ---------------------------------------------------------------------------
# POST /cashbook/quotes/new
# ---------------------------------------------------------------------------


@router.post("/cashbook/quotes/new", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_create(request: Request) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    idempotency_key = form.get("idempotency_key") or str(uuid.uuid4())
    customer_name = form.get("customer_name", "").strip()
    customer_email = form.get("customer_email", "").strip() or None
    description = form.get("description", "").strip() or "Services"
    amount_str = form.get("amount", "").strip()
    gst_on = form.get("gst", "").strip().lower() in ("on", "true", "1", "yes")
    issue_date = form.get("issue_date", date.today().isoformat()).strip()
    expiry_date = form.get("expiry_date", "").strip() or (
        date.today() + timedelta(days=28)
    ).isoformat()

    errors: dict[str, str] = {}
    if not customer_name:
        errors["customer_name"] = "Required."
    amount = _money(amount_str)
    if amount is None or amount <= 0:
        errors["amount"] = "Enter an amount."

    def _render(errors: dict[str, str], status: int = 400) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request,
            "cashbook/quotes/new.html",
            {
                "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
                "form": {
                    "customer_name": customer_name,
                    "customer_email": customer_email or "",
                    "description": description,
                    "amount": amount_str,
                    "gst": "on" if gst_on else "",
                    "issue_date": issue_date,
                    "expiry_date": expiry_date,
                },
                "errors": errors,
                "idempotency_key": idempotency_key,
            },
            status_code=status,
        )

    if errors:
        return _render(errors)

    contact_id, contact_err = await _lookup_or_create_contact(
        request, name=customer_name, email=customer_email
    )
    if contact_err or not contact_id:
        return _render({"__all__": contact_err or "Could not resolve customer."})
    income_account_id = await _first_income_account_id(request)
    if not income_account_id:
        return _render({"__all__": "No INCOME account configured."})
    tax_code_label = "GST" if gst_on else "FRE"
    tax_code_id = await _tax_code_id(request, tax_code_label)
    if not tax_code_id:
        return _render({"__all__": f"Tax code '{tax_code_label}' not found."})

    if gst_on:
        unit_price = (amount / Decimal("1.10")).quantize(Decimal("0.01"))
    else:
        unit_price = amount.quantize(Decimal("0.01"))

    payload = {
        "customer_id": contact_id,
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "lines": [
            {
                "description": description,
                "account_id": income_account_id,
                "tax_code_id": tax_code_id,
                "quantity": "1",
                "unit_price": str(unit_price),
            }
        ],
    }

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/quotes",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        request.session["flash"] = "Quote created — review and send it."
        return RedirectResponse(
            url=f"/cashbook/quotes/{created['id']}", status_code=303
        )

    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list) and detail:
        return _render({"__all__": detail[0].get("msg", f"API error: HTTP {resp.status_code}")})
    if isinstance(detail, str):
        return _render({"__all__": detail})
    return _render({"__all__": f"API error: HTTP {resp.status_code}"})


# ---------------------------------------------------------------------------
# GET /cashbook/quotes/{id}
# ---------------------------------------------------------------------------


@router.get("/cashbook/quotes/{quote_id}", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_detail(
    request: Request, quote_id: str
) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/quotes/{quote_id}")
        if resp.status_code == 404:
            request.session["flash"] = "Quote not found."
            return RedirectResponse(url="/cashbook/quotes", status_code=303)
        if not resp.is_success:
            request.session["flash"] = f"Could not load quote (HTTP {resp.status_code})."
            return RedirectResponse(url="/cashbook/quotes", status_code=303)
        quote = resp.json()

        contact = None
        contact_id = quote.get("customer_id")
        if contact_id:
            c_resp = await client.get(f"/api/v1/contacts/{contact_id}")
            if c_resp.is_success:
                contact = c_resp.json()

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/quotes/detail.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "quote": quote,
            "contact": contact,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


async def _transition(
    request: Request, quote_id: str, action: str
) -> RedirectResponse:
    """POST /api/v1/quotes/{id}/{action} with If-Match. Flashes outcome."""
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/quotes/{quote_id}/{action}",
            headers={"If-Match": version, "X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 200:
        labels = {
            "send": "Quote sent.",
            "accept": "Quote accepted.",
            "decline": "Quote declined.",
        }
        request.session["flash"] = labels.get(action, "Done.")
    elif resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
    else:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", f"API error: HTTP {resp.status_code}")
        request.session["flash"] = str(detail or f"API error: HTTP {resp.status_code}")
    return RedirectResponse(url=f"/cashbook/quotes/{quote_id}", status_code=303)


@router.post("/cashbook/quotes/{quote_id}/send", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_send(request: Request, quote_id: str) -> RedirectResponse:
    return await _transition(request, quote_id, "send")


@router.post("/cashbook/quotes/{quote_id}/accept", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_accept(request: Request, quote_id: str) -> RedirectResponse:
    return await _transition(request, quote_id, "accept")


@router.post("/cashbook/quotes/{quote_id}/decline", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_decline(request: Request, quote_id: str) -> RedirectResponse:
    return await _transition(request, quote_id, "decline")


@router.post("/cashbook/quotes/{quote_id}/convert", response_class=HTMLResponse, response_model=None)
async def cashbook_quote_convert(
    request: Request, quote_id: str
) -> RedirectResponse:
    """ACCEPTED → INVOICED. Redirect to the resulting invoice in cashbook."""
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/quotes/{quote_id}/convert-to-invoice",
            headers={"If-Match": version, "X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 200:
        body = resp.json()
        invoice_id = body.get("invoice_id")
        request.session["flash"] = "Quote converted to invoice."
        if invoice_id:
            return RedirectResponse(
                url=f"/cashbook/invoices/{invoice_id}", status_code=303
            )
    elif resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
    else:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", f"API error: HTTP {resp.status_code}")
        request.session["flash"] = str(detail or f"API error: HTTP {resp.status_code}")
    return RedirectResponse(url=f"/cashbook/quotes/{quote_id}", status_code=303)


# ---------------------------------------------------------------------------
# Helpers — derive cashbook form values from a stored quote
# ---------------------------------------------------------------------------


def _form_from_quote(
    quote: dict, contact: dict | None
) -> dict[str, str]:
    line = (quote.get("lines") or [{}])[0]
    description = line.get("description") or "Services"
    try:
        total = Decimal(str(quote.get("total") or "0"))
    except (InvalidOperation, TypeError):
        total = Decimal("0")
    try:
        tax_total = Decimal(str(quote.get("tax_total") or "0"))
    except (InvalidOperation, TypeError):
        tax_total = Decimal("0")
    gst_on = tax_total > 0
    return {
        "customer_name": (contact or {}).get("name") or "",
        "customer_email": (contact or {}).get("email") or "",
        "description": description,
        "amount": f"{total:.2f}" if total else "",
        "gst": "on" if gst_on else "",
        "issue_date": quote.get("issue_date") or date.today().isoformat(),
        "expiry_date": quote.get("expiry_date")
        or (date.today() + timedelta(days=28)).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /cashbook/quotes/{id}/edit — DRAFT or SENT only
# ---------------------------------------------------------------------------


@router.get(
    "/cashbook/quotes/{quote_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_quote_edit(
    request: Request, quote_id: str
) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/quotes/{quote_id}")
        if resp.status_code == 404:
            request.session["flash"] = "Quote not found."
            return RedirectResponse(url="/cashbook/quotes", status_code=303)
        if not resp.is_success:
            request.session["flash"] = (
                f"Could not load quote (HTTP {resp.status_code})."
            )
            return RedirectResponse(url="/cashbook/quotes", status_code=303)
        quote = resp.json()

        contact = None
        contact_id = quote.get("customer_id")
        if contact_id:
            c_resp = await client.get(f"/api/v1/contacts/{contact_id}")
            if c_resp.is_success:
                contact = c_resp.json()

    if quote.get("status") not in ("DRAFT", "SENT"):
        request.session["flash"] = (
            f"{quote.get('status', '').title()} quotes are read-only."
        )
        return RedirectResponse(
            url=f"/cashbook/quotes/{quote_id}", status_code=303
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/quotes/edit.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "quote": quote,
            "form": _form_from_quote(quote, contact),
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
        },
    )


# ---------------------------------------------------------------------------
# POST /cashbook/quotes/{id}/edit — submit edit
# ---------------------------------------------------------------------------


@router.post(
    "/cashbook/quotes/{quote_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_quote_update(
    request: Request, quote_id: str
) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    idempotency_key = form.get("idempotency_key") or str(uuid.uuid4())
    customer_name = form.get("customer_name", "").strip()
    customer_email = form.get("customer_email", "").strip() or None
    description = form.get("description", "").strip() or "Services"
    amount_str = form.get("amount", "").strip()
    gst_on = form.get("gst", "").strip().lower() in ("on", "true", "1", "yes")
    issue_date = form.get("issue_date", date.today().isoformat()).strip()
    expiry_date = form.get("expiry_date", "").strip() or (
        date.today() + timedelta(days=28)
    ).isoformat()
    version_str = form.get("version", "").strip()

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/quotes/{quote_id}")
        if resp.status_code == 404:
            request.session["flash"] = "Quote not found."
            return RedirectResponse(url="/cashbook/quotes", status_code=303)
        if not resp.is_success:
            request.session["flash"] = (
                f"Could not load quote (HTTP {resp.status_code})."
            )
            return RedirectResponse(url="/cashbook/quotes", status_code=303)
        quote = resp.json()

    if quote.get("status") not in ("DRAFT", "SENT"):
        request.session["flash"] = (
            f"{quote.get('status', '').title()} quotes are read-only."
        )
        return RedirectResponse(
            url=f"/cashbook/quotes/{quote_id}", status_code=303
        )

    errors: dict[str, str] = {}
    if not customer_name:
        errors["customer_name"] = "Required."
    amount = _money(amount_str)
    if amount is None or amount <= 0:
        errors["amount"] = "Enter an amount."

    def _render(errs: dict[str, str], status: int = 400) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request,
            "cashbook/quotes/edit.html",
            {
                "company": company,
                "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
                "quote": quote,
                "form": {
                    "customer_name": customer_name,
                    "customer_email": customer_email or "",
                    "description": description,
                    "amount": amount_str,
                    "gst": "on" if gst_on else "",
                    "issue_date": issue_date,
                    "expiry_date": expiry_date,
                },
                "errors": errs,
                "idempotency_key": idempotency_key,
            },
            status_code=status,
        )

    if errors:
        return _render(errors)

    contact_id, contact_err = await _lookup_or_create_contact(
        request, name=customer_name, email=customer_email
    )
    if contact_err or not contact_id:
        return _render({"__all__": contact_err or "Could not resolve customer."})
    income_account_id = await _first_income_account_id(request)
    if not income_account_id:
        return _render({"__all__": "No INCOME account configured."})
    tax_code_label = "GST" if gst_on else "FRE"
    tax_code_id = await _tax_code_id(request, tax_code_label)
    if not tax_code_id:
        return _render({"__all__": f"Tax code '{tax_code_label}' not found."})

    if gst_on:
        unit_price = (amount / Decimal("1.10")).quantize(Decimal("0.01"))
    else:
        unit_price = amount.quantize(Decimal("0.01"))

    payload = {
        "customer_id": contact_id,
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "lines": [
            {
                "description": description,
                "account_id": income_account_id,
                "tax_code_id": tax_code_id,
                "quantity": "1",
                "unit_price": str(unit_price),
            }
        ],
    }

    version_header = version_str or str(quote.get("version", ""))
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/quotes/{quote_id}",
            json=payload,
            headers={
                "If-Match": version_header,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 200:
        request.session["flash"] = "Quote updated."
        return RedirectResponse(
            url=f"/cashbook/quotes/{quote_id}", status_code=303
        )
    if resp.status_code == 409:
        return _render(
            {"__all__": "Someone else updated this quote — reload and try again."},
            status=409,
        )

    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list) and detail:
        return _render({"__all__": detail[0].get("msg", f"API error: HTTP {resp.status_code}")})
    if isinstance(detail, str):
        return _render({"__all__": detail})
    return _render({"__all__": f"API error: HTTP {resp.status_code}"})
