"""Money on account and refunds (engine migration 0227).

GET  /payments/{id}/apply-panel  — HTMX fragment on the payment page: what is
                                   still on account and a form to apply it to
                                   the contact's open invoices / bills.
POST /payments/{id}/allocate     — apply it (engine POST /payments/{id}/allocations).
POST /payments/{id}/unallocate   — the Unmatch button (engine DELETE
                                   /payments/{id}/allocations/{allocation_id});
                                   before this the button posted to a route
                                   that did not exist (gap P0-3).
GET  /refunds/new?source=&id=    — refund form for a customer credit note, a
                                   supplier credit note or a payment on account.
POST /refunds/new                — creates the refund as a DRAFT payment
                                   allocated to the source, then opens it so the
                                   user checks and posts it (gap P0-5).

Kept in its own module so the payment screens (owned by another batch) only
gain one include line.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web import credit_apply
from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_METHODS = [
    ("eft", "Bank transfer"),
    ("direct_deposit", "Direct deposit"),
    ("card", "Card"),
    ("cheque", "Cheque"),
    ("cash", "Cash"),
    ("other", "Other"),
]


def _authed(request: Request) -> bool:
    return bool(request.session.get("api_token"))


@router.get("/payments/{payment_id}/apply-panel", response_class=HTMLResponse, response_model=None)
async def payment_apply_panel(request: Request, payment_id: str) -> HTMLResponse:
    if not _authed(request):
        return HTMLResponse("")
    async with api_client(request) as client:
        try:
            resp = await client.get(f"/api/v1/payments/{payment_id}")
        except Exception:
            return HTMLResponse("")
    if not resp.is_success:
        return HTMLResponse("")
    payment = resp.json()
    if payment.get("amount_unallocated") is None:
        return HTMLResponse("")  # engine predates 0227
    side = credit_apply.payment_side(payment)
    capp = await credit_apply.load_context(request, side, payment)
    return _TEMPLATES.TemplateResponse(
        request,
        "payments/_apply_panel.html",
        {"payment": payment, "capp": capp},
    )


@router.post("/payments/{payment_id}/allocate", response_class=HTMLResponse, response_model=None)
async def payment_allocate(request: Request, payment_id: str) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    side = (
        credit_apply.PAYMENT_IN
        if str(form.get("direction") or "").upper() == "INCOMING"
        else credit_apply.PAYMENT_OUT
    )
    return await credit_apply.submit_apply(request, side, payment_id)


@router.post("/payments/{payment_id}/unallocate", response_class=HTMLResponse, response_model=None)
async def payment_unallocate(request: Request, payment_id: str) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    allocation_id = str(form.get("allocation_id") or "")
    try:
        uuid.UUID(allocation_id)
    except ValueError:
        request.session["flash"] = "Unmatch failed: no allocation given."
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)
    return await credit_apply.submit_unapply(
        request, credit_apply.PAYMENT_IN, payment_id, allocation_id
    )


async def _bank_accounts(client) -> list[dict]:
    try:
        r = await client.get("/api/v1/bank_accounts", params={"page": 1, "page_size": 200})
        if r.is_success:
            return r.json().get("items", []) or []
    except Exception:
        pass
    return []


@router.get("/refunds/new", response_class=HTMLResponse, response_model=None)
async def refund_new(request: Request, source: str = "", id: str = "") -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    if source not in credit_apply.REFUND_SOURCES:
        return HTMLResponse("Unknown refund source", status_code=400)
    api_path = credit_apply.REFUND_SOURCES[source][0]
    async with api_client(request) as client:
        resp = await client.get(f"{api_path}/{id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if not resp.is_success:
            return HTMLResponse("Not found", status_code=404)
        record = resp.json()
        contact = None
        try:
            cr = await client.get(f"/api/v1/contacts/{record.get('contact_id')}")
            if cr.is_success:
                contact = cr.json()
        except Exception:
            contact = None
        banks = await _bank_accounts(client)
    plan = credit_apply.refund_plan(source, record)
    return _TEMPLATES.TemplateResponse(
        request,
        "refunds/new.html",
        {
            "source": source,
            "record": record,
            "contact": contact,
            "plan": plan,
            "bank_accounts": banks,
            "methods": _METHODS,
            "today": date.today().isoformat(),
            "idem": str(uuid.uuid4()),
            "error": request.session.pop("flash", None),
        },
    )


@router.post("/refunds/new", response_class=HTMLResponse, response_model=None)
async def refund_create(request: Request) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    source = str(form.get("source") or "")
    source_id = str(form.get("source_id") or "")
    back = f"/refunds/new?source={source}&id={source_id}"
    if source not in credit_apply.REFUND_SOURCES:
        request.session["flash"] = "Unknown refund source."
        return RedirectResponse(url=back, status_code=303)
    api_path = credit_apply.REFUND_SOURCES[source][0]
    try:
        amount = Decimal(str(form.get("amount") or "").replace(",", ""))
    except InvalidOperation:
        amount = Decimal("0")
    if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        request.session["flash"] = "Enter the amount refunded (to the cent)."
        return RedirectResponse(url=back, status_code=303)
    async with api_client(request) as client:
        src = await client.get(f"{api_path}/{source_id}")
        if not src.is_success:
            request.session["flash"] = credit_apply.business_error(src)
            return RedirectResponse(url=back, status_code=303)
        record = src.json()
        plan = credit_apply.refund_plan(source, record)
        body = {
            "contact_id": record.get("contact_id"),
            "bank_account_id": str(form.get("bank_account_id") or ""),
            "payment_date": str(form.get("payment_date") or date.today().isoformat()),
            "amount": str(amount),
            "direction": plan["direction"],
            "method": str(form.get("method") or "eft"),
            "reference": (str(form.get("reference") or "").strip() or None),
            "notes": (str(form.get("notes") or "").strip() or None),
            "allocations": [{plan["key"]: source_id, "amount": str(amount)}],
        }
        resp = await client.post(
            "/api/v1/payments",
            json=body,
            headers={"X-Idempotency-Key": str(form.get("idem") or uuid.uuid4())},
        )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        new_id = resp.json().get("id")
        request.session["flash"] = (
            "Refund saved as a draft payment. Check the bank account and date, "
            "then Post it."
        )
        return RedirectResponse(url=f"/payments/{new_id}", status_code=303)
    request.session["flash"] = credit_apply.business_error(resp)
    return RedirectResponse(url=back, status_code=303)
