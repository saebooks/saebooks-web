"""Cashbook UI — phone-first invoicing for sole traders.

Routes
------
GET  /cashbook/invoices              — list (open + sent + paid)
GET  /cashbook/invoices/new          — simple one-line form
POST /cashbook/invoices/new          — submit (lookup-or-create contact, single line, GST toggle)
GET  /cashbook/invoices/{id}         — detail page
POST /cashbook/invoices/{id}/send    — DRAFT → POSTED (idempotent via If-Match)
POST /cashbook/invoices/{id}/void    — POSTED → VOIDED

Phone-first UI: customer is free-text (name + email), no dropdowns. Income account
is auto-picked (first INCOME by code). GST is a single checkbox: ON → "GST" tax
code, OFF → "FRE". Net 14 default due date. Single line by default; description
defaults to "Services".
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

logger = logging.getLogger("saebooks_web.cashbook_invoices")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Auth + cashbook-mode guard (mirrors routes/cashbook.py)
# ---------------------------------------------------------------------------


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


async def _get_active_company(request: Request) -> dict | None:
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if resp.is_success:
            items = resp.json().get("items", [])
            if items:
                return items[0]
    except Exception:
        pass
    return None


async def _guard(request: Request) -> tuple[dict | None, RedirectResponse | None]:
    """Return (company, redirect). If redirect is non-None, caller should return it."""
    if not _require_auth(request):
        return None, RedirectResponse(url="/login", status_code=303)
    company = await _get_active_company(request)
    if not company or company.get("bookkeeping_mode") != "cashbook":
        request.session["flash"] = "This page is for Cashbook companies only."
        return None, RedirectResponse(url="/", status_code=303)
    return company, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _money(s: str) -> Decimal | None:
    try:
        return Decimal(s.strip())
    except (InvalidOperation, AttributeError, TypeError):
        return None


async def _lookup_or_create_contact(
    request: Request, *, name: str, email: str | None
) -> tuple[str | None, str | None]:
    """Find an existing CUSTOMER contact by email or exact name; create if absent.

    Returns (contact_id, error_message).
    """
    name = (name or "").strip()
    email = (email or "").strip() or None
    if not name:
        return None, "Customer name is required."

    async with api_client(request) as client:
        # Search via list endpoint (q matches name OR email ilike). Check both
        # CUSTOMER and BOTH contact_types so an existing dual-role contact is
        # matched here instead of duplicated.
        for _ctype in ("CUSTOMER", "BOTH"):
            params = {"type": _ctype, "limit": 50, "offset": 0}
            if email:
                params["q"] = email
            else:
                params["q"] = name
            resp = await client.get("/api/v1/contacts", params=params)
            if resp.is_success:
                for c in resp.json().get("items", []):
                    if email and (c.get("email") or "").strip().lower() == email.lower():
                        return c["id"], None
                    if (c.get("name") or "").strip().lower() == name.lower():
                        return c["id"], None

        # Not found — create.
        payload: dict[str, object] = {"name": name, "contact_type": "CUSTOMER"}
        if email:
            payload["email"] = email
        idempotency_key = f"cashbook-contact-{uuid.uuid4()}"
        c_resp = await client.post(
            "/api/v1/contacts",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        if c_resp.status_code == 201:
            return c_resp.json()["id"], None
        return None, f"Could not save customer (HTTP {c_resp.status_code})."


async def _first_income_account_id(request: Request) -> str | None:
    """Return the first INCOME-type account id (lowest code)."""
    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/accounts",
            params={"limit": 200, "offset": 0},
        )
    if not resp.is_success:
        return None
    items = resp.json().get("items", [])
    income = [a for a in items if a.get("account_type") == "INCOME"]
    income.sort(key=lambda a: (a.get("code") or "", a.get("name") or ""))
    return income[0]["id"] if income else None


async def _tax_code_id(request: Request, code: str) -> str | None:
    async with api_client(request) as client:
        resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if not resp.is_success:
        return None
    for tc in resp.json().get("items", []):
        if (tc.get("code") or "").upper() == code.upper():
            return tc["id"]
    return None


# ---------------------------------------------------------------------------
# GET /cashbook/invoices — list
# ---------------------------------------------------------------------------


@router.get("/cashbook/invoices", response_class=HTMLResponse, response_model=None)
async def cashbook_invoices_list(request: Request) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    invoices: list[dict] = []
    contacts_by_id: dict[str, dict] = {}
    async with api_client(request) as client:
        resp = await client.get("/api/v1/invoices", params={"limit": 100, "offset": 0})
        if resp.is_success:
            invoices = resp.json().get("items", [])
        for _ctype in ("CUSTOMER", "BOTH"):
            c_resp = await client.get(
                "/api/v1/contacts",
                params={"type": _ctype, "limit": 200, "offset": 0},
            )
            if c_resp.is_success:
                for c in c_resp.json().get("items", []):
                    contacts_by_id[c["id"]] = c

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/invoices/list.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "invoices": invoices,
            "contacts_by_id": contacts_by_id,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# GET /cashbook/invoices/new — empty form
# ---------------------------------------------------------------------------


@router.get("/cashbook/invoices/new", response_class=HTMLResponse, response_model=None)
async def cashbook_invoice_new(request: Request) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    today = date.today().isoformat()
    due = (date.today() + timedelta(days=14)).isoformat()
    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/invoices/new.html",
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
                "due_date": due,
            },
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
        },
    )


# ---------------------------------------------------------------------------
# POST /cashbook/invoices/new — submit
# ---------------------------------------------------------------------------


@router.post("/cashbook/invoices/new", response_class=HTMLResponse, response_model=None)
async def cashbook_invoice_create(request: Request) -> HTMLResponse | RedirectResponse:
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
    due_date = form.get("due_date", "").strip() or (
        date.today() + timedelta(days=14)
    ).isoformat()

    errors: dict[str, str] = {}
    if not customer_name:
        errors["customer_name"] = "Required."
    amount = _money(amount_str)
    if amount is None or amount <= 0:
        errors["amount"] = "Enter an amount."

    if errors:
        return _TEMPLATES.TemplateResponse(
            request,
            "cashbook/invoices/new.html",
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
                    "due_date": due_date,
                },
                "errors": errors,
                "idempotency_key": idempotency_key,
            },
            status_code=400,
        )

    contact_id, contact_err = await _lookup_or_create_contact(
        request, name=customer_name, email=customer_email
    )
    if contact_err or not contact_id:
        errors["__all__"] = contact_err or "Could not resolve customer."
    income_account_id = await _first_income_account_id(request)
    if not income_account_id:
        errors["__all__"] = "No INCOME account configured. Set up the chart first."
    tax_code_label = "GST" if gst_on else "FRE"
    tax_code_id = await _tax_code_id(request, tax_code_label)
    if not tax_code_id:
        errors["__all__"] = f"Tax code '{tax_code_label}' not found in this company."

    if errors:
        return _TEMPLATES.TemplateResponse(
            request,
            "cashbook/invoices/new.html",
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
                    "due_date": due_date,
                },
                "errors": errors,
                "idempotency_key": idempotency_key,
            },
            status_code=400,
        )

    # Amount is treated as GST-inclusive when GST is on (1/11 belongs to ATO).
    # Convert to net unit_price so the invoice line totals to the gross amount.
    if gst_on:
        unit_price = (amount / Decimal("1.10")).quantize(Decimal("0.01"))
    else:
        unit_price = amount.quantize(Decimal("0.01"))

    payload = {
        "contact_id": contact_id,
        "issue_date": issue_date,
        "due_date": due_date,
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
            "/api/v1/invoices",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        request.session["flash"] = "Invoice created — review and send it."
        return RedirectResponse(
            url=f"/cashbook/invoices/{created['id']}", status_code=303
        )

    # Validation or other error.
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list) and detail:
        errors["__all__"] = detail[0].get("msg", f"API error: HTTP {resp.status_code}")
    elif isinstance(detail, str):
        errors["__all__"] = detail
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/invoices/new.html",
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
                "due_date": due_date,
            },
            "errors": errors,
            "idempotency_key": idempotency_key,
        },
        status_code=400,
    )


# ---------------------------------------------------------------------------
# GET /cashbook/invoices/{id} — detail
# ---------------------------------------------------------------------------


@router.get("/cashbook/invoices/{invoice_id}", response_class=HTMLResponse, response_model=None)
async def cashbook_invoice_detail(
    request: Request, invoice_id: str
) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/invoices/{invoice_id}")
        if resp.status_code == 404:
            request.session["flash"] = "Invoice not found."
            return RedirectResponse(url="/cashbook/invoices", status_code=303)
        if not resp.is_success:
            request.session["flash"] = f"Could not load invoice (HTTP {resp.status_code})."
            return RedirectResponse(url="/cashbook/invoices", status_code=303)
        invoice = resp.json()

        contact = None
        contact_id = invoice.get("contact_id")
        if contact_id:
            c_resp = await client.get(f"/api/v1/contacts/{contact_id}")
            if c_resp.is_success:
                contact = c_resp.json()

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/invoices/detail.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "invoice": invoice,
            "contact": contact,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /cashbook/invoices/{id}/send — DRAFT → POSTED
# ---------------------------------------------------------------------------


@router.post("/cashbook/invoices/{invoice_id}/send", response_class=HTMLResponse, response_model=None)
async def cashbook_invoice_send(
    request: Request, invoice_id: str
) -> RedirectResponse:
    _company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/post",
            headers={"If-Match": version, "X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 200:
        request.session["flash"] = "Invoice sent."
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
    return RedirectResponse(url=f"/cashbook/invoices/{invoice_id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /cashbook/invoices/{id}/void
# ---------------------------------------------------------------------------


@router.post("/cashbook/invoices/{invoice_id}/void", response_class=HTMLResponse, response_model=None)
async def cashbook_invoice_void(
    request: Request, invoice_id: str
) -> RedirectResponse:
    _company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/void",
            headers={"If-Match": version, "X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 200:
        request.session["flash"] = "Invoice voided."
    else:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", f"API error: HTTP {resp.status_code}")
        request.session["flash"] = str(detail or f"API error: HTTP {resp.status_code}")
    return RedirectResponse(url=f"/cashbook/invoices/{invoice_id}", status_code=303)


# ---------------------------------------------------------------------------
# Helpers — derive cashbook form values from a stored invoice
# ---------------------------------------------------------------------------


def _form_from_invoice(
    invoice: dict, contact: dict | None
) -> dict[str, str]:
    """Reverse-engineer the cashbook 'simple form' values from a stored invoice.

    The cashbook flow only ever creates single-line invoices with one tax code
    (GST or FRE), and the amount entered is the *gross* (GST-inclusive) total.
    Inverting that here keeps the edit form consistent with the create form.
    """
    line = (invoice.get("lines") or [{}])[0]
    description = line.get("description") or "Services"
    try:
        total = Decimal(str(invoice.get("total") or "0"))
    except (InvalidOperation, TypeError):
        total = Decimal("0")
    try:
        tax_total = Decimal(str(invoice.get("tax_total") or "0"))
    except (InvalidOperation, TypeError):
        tax_total = Decimal("0")
    gst_on = tax_total > 0
    return {
        "customer_name": (contact or {}).get("name") or "",
        "customer_email": (contact or {}).get("email") or "",
        "description": description,
        "amount": f"{total:.2f}" if total else "",
        "gst": "on" if gst_on else "",
        "issue_date": invoice.get("issue_date") or date.today().isoformat(),
        "due_date": invoice.get("due_date")
        or (date.today() + timedelta(days=14)).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /cashbook/invoices/{id}/edit — DRAFT only
# ---------------------------------------------------------------------------


@router.get(
    "/cashbook/invoices/{invoice_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_invoice_edit(
    request: Request, invoice_id: str
) -> HTMLResponse | RedirectResponse:
    company, redirect = await _guard(request)
    if redirect is not None:
        return redirect

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/invoices/{invoice_id}")
        if resp.status_code == 404:
            request.session["flash"] = "Invoice not found."
            return RedirectResponse(url="/cashbook/invoices", status_code=303)
        if not resp.is_success:
            request.session["flash"] = (
                f"Could not load invoice (HTTP {resp.status_code})."
            )
            return RedirectResponse(url="/cashbook/invoices", status_code=303)
        invoice = resp.json()

        contact = None
        contact_id = invoice.get("contact_id")
        if contact_id:
            c_resp = await client.get(f"/api/v1/contacts/{contact_id}")
            if c_resp.is_success:
                contact = c_resp.json()

    if invoice.get("status") != "DRAFT":
        request.session["flash"] = (
            "Sent invoices can't be edited — Void it and create a new one "
            "(ATO requires the audit trail)."
        )
        return RedirectResponse(
            url=f"/cashbook/invoices/{invoice_id}", status_code=303
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "cashbook/invoices/edit.html",
        {
            "company": company,
            "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
            "invoice": invoice,
            "form": _form_from_invoice(invoice, contact),
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
        },
    )


# ---------------------------------------------------------------------------
# POST /cashbook/invoices/{id}/edit — submit edit (DRAFT only)
# ---------------------------------------------------------------------------


@router.post(
    "/cashbook/invoices/{invoice_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def cashbook_invoice_update(
    request: Request, invoice_id: str
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
    due_date = form.get("due_date", "").strip() or (
        date.today() + timedelta(days=14)
    ).isoformat()
    version_str = form.get("version", "").strip()

    # Re-fetch invoice for status guard + render-on-error context.
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/invoices/{invoice_id}")
        if resp.status_code == 404:
            request.session["flash"] = "Invoice not found."
            return RedirectResponse(url="/cashbook/invoices", status_code=303)
        if not resp.is_success:
            request.session["flash"] = (
                f"Could not load invoice (HTTP {resp.status_code})."
            )
            return RedirectResponse(url="/cashbook/invoices", status_code=303)
        invoice = resp.json()

    if invoice.get("status") != "DRAFT":
        request.session["flash"] = (
            "Sent invoices can't be edited — Void it and create a new one."
        )
        return RedirectResponse(
            url=f"/cashbook/invoices/{invoice_id}", status_code=303
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
            "cashbook/invoices/edit.html",
            {
                "company": company,
                "bookkeeping_mode": (company or {}).get("bookkeeping_mode", "cashbook"),
                "invoice": invoice,
                "form": {
                    "customer_name": customer_name,
                    "customer_email": customer_email or "",
                    "description": description,
                    "amount": amount_str,
                    "gst": "on" if gst_on else "",
                    "issue_date": issue_date,
                    "due_date": due_date,
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
        "contact_id": contact_id,
        "issue_date": issue_date,
        "due_date": due_date,
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

    version_header = version_str or str(invoice.get("version", ""))
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/invoices/{invoice_id}",
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
        request.session["flash"] = "Invoice updated."
        return RedirectResponse(
            url=f"/cashbook/invoices/{invoice_id}", status_code=303
        )
    if resp.status_code == 409:
        return _render(
            {"__all__": "Someone else updated this invoice — reload and try again."},
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
