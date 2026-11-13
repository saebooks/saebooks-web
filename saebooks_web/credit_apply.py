"""Apply a posted credit note to open documents — shared by the supplier
credit note page (credit -> bills) and the customer credit note page
(credit -> invoices).

Engine endpoints (migration 0227, engine branch feat/supplier-credit-apply):

* ``GET    /api/v1/{credits}/{id}``                        — carries
  ``amount_applied``, ``amount_remaining`` and ``applications``;
* ``POST   /api/v1/{credits}/{id}/applications``           — If-Match +
  X-Idempotency-Key, body ``{applied_date, notes, allocations: [{<doc>_id,
  amount}]}``;
* ``DELETE /api/v1/{credits}/{id}/applications/{app_id}``  — If-Match.

Applying posts no journal; the engine just records which documents the
credit settled. The form lists the contact's open documents oldest first and
prefills amounts by spending the remaining credit down that list, so the
common case ("use the credit on the oldest bills") is one click.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse

from saebooks_web.api_client import api_client

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Side:
    key: str  # "supplier" | "customer"
    credit_api: str  # /api/v1/supplier_credit_notes
    credit_web: str  # /supplier-credit-notes
    doc_api: str  # /api/v1/bills
    doc_web: str  # /bills
    doc_key: str  # bill_id
    doc_label: str  # bill
    doc_label_plural: str  # bills
    ref_field: str | None  # supplier_reference on bills; invoices have none
    apply_path: str = "applications"  # payments: "allocations"
    is_payment: bool = False


SUPPLIER = Side(
    key="supplier",
    credit_api="/api/v1/supplier_credit_notes",
    credit_web="/supplier-credit-notes",
    doc_api="/api/v1/bills",
    doc_web="/bills",
    doc_key="bill_id",
    doc_label="bill",
    doc_label_plural="bills",
    ref_field="supplier_reference",
)

CUSTOMER = Side(
    key="customer",
    credit_api="/api/v1/credit_notes",
    credit_web="/credit-notes",
    doc_api="/api/v1/invoices",
    doc_web="/invoices",
    doc_key="invoice_id",
    doc_label="invoice",
    doc_label_plural="invoices",
    ref_field=None,
)

# Money on account on a POSTED payment (engine 0227, gap P0-3): a receipt is
# applied to the customer's invoices, a supplier payment to their bills.
PAYMENT_IN = Side(
    key="payment_in",
    credit_api="/api/v1/payments",
    credit_web="/payments",
    doc_api="/api/v1/invoices",
    doc_web="/invoices",
    doc_key="invoice_id",
    doc_label="invoice",
    doc_label_plural="invoices",
    ref_field=None,
    apply_path="allocations",
    is_payment=True,
)
PAYMENT_OUT = Side(
    key="payment_out",
    credit_api="/api/v1/payments",
    credit_web="/payments",
    doc_api="/api/v1/bills",
    doc_web="/bills",
    doc_key="bill_id",
    doc_label="bill",
    doc_label_plural="bills",
    ref_field="supplier_reference",
    apply_path="allocations",
    is_payment=True,
)


def payment_side(payment: dict) -> Side:
    return PAYMENT_IN if str(payment.get("direction") or "").upper() == "INCOMING" else PAYMENT_OUT


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        return _ZERO


def remaining_of(credit: dict) -> Decimal:
    """Remaining credit as the engine reports it; falls back to total minus
    applied (minus refunded on the customer side) for an older engine."""
    if credit.get("amount_remaining") is not None:
        return _dec(credit["amount_remaining"])
    if credit.get("amount_unallocated") is not None:  # a payment
        return _dec(credit["amount_unallocated"])
    return (
        _dec(credit.get("total"))
        - _dec(credit.get("amount_applied"))
        - _dec(credit.get("amount_allocated"))
    )


def open_documents(side: Side, items: list[dict], remaining: Decimal) -> list[dict]:
    """POSTED documents with something outstanding, oldest first, each with a
    suggested amount that spends ``remaining`` down the list."""
    docs = []
    for d in items:
        if str(d.get("status") or "").upper() != "POSTED":
            continue
        outstanding = _dec(d.get("total")) - _dec(d.get("amount_paid"))
        if outstanding <= _ZERO:
            continue
        docs.append({
            "id": d["id"],
            "number": d.get("number") or "(no number)",
            "reference": d.get(side.ref_field) if side.ref_field else None,
            "issue_date": d.get("issue_date") or "",
            "due_date": d.get("due_date") or "",
            "total": _dec(d.get("total")),
            "amount_paid": _dec(d.get("amount_paid")),
            "outstanding": outstanding,
        })
    docs.sort(key=lambda d: (d["issue_date"], d["number"]))
    left = max(remaining, _ZERO)
    for d in docs:
        take = min(left, d["outstanding"])
        d["suggested"] = take.quantize(_CENT)
        left -= take
    return docs


async def load_context(request: Request, side: Side, credit: dict) -> dict:
    """Contact + open documents for the apply form. Every extra call is
    best-effort: a failure leaves the page usable without the form."""
    contact = None
    docs: list[dict] = []
    status = str(credit.get("status") or "").upper()
    remaining = remaining_of(credit)
    contact_id = credit.get("contact_id")
    async with api_client(request) as client:
        if contact_id:
            try:
                r = await client.get(f"/api/v1/contacts/{contact_id}")
                if r.is_success:
                    contact = r.json()
            except Exception:  # optional enrichment
                contact = None
        if status == "POSTED" and remaining > _ZERO and contact_id:
            try:
                r = await client.get(
                    side.doc_api,
                    params={"contact_id": contact_id, "status": "POSTED",
                            "page": 1, "page_size": 500},
                )
                if r.is_success:
                    docs = open_documents(side, r.json().get("items", []) or [], remaining)
            except Exception:  # optional enrichment
                docs = []
    return {
        "side": side,
        "contact": contact,
        "open_docs": docs,
        "remaining": remaining,
        "applied": _dec(credit.get("amount_applied")),
        "applications": credit.get("applications") or [],
        "today": date.today().isoformat(),
        "idem": str(uuid.uuid4()),
        "refund_url": refund_url(side, credit) if status == "POSTED" and remaining > _ZERO else None,
    }


def refund_url(side: Side, credit: dict) -> str | None:
    """Where the "Record refund" button goes for this credit, if anywhere."""
    if side is SUPPLIER:
        source = "supplier_credit_note"
    elif side is CUSTOMER:
        source = "credit_note"
    elif side.is_payment:
        source = "payment"
    else:
        return None
    return f"/refunds/new?source={source}&id={credit.get('id')}"


# --- refunds (engine 0227, gap P0-5) ---------------------------------------
#
# source            refund direction   allocation key
# credit_note       OUTGOING           credit_note_id          (Dr Trade Debtors)
# supplier_credit_note INCOMING        supplier_credit_note_id (Cr Trade Creditors)
# payment INCOMING  OUTGOING           refund_of_payment_id    (Dr Trade Debtors)
# payment OUTGOING  INCOMING           refund_of_payment_id    (Cr Trade Creditors)
REFUND_SOURCES = {
    "credit_note": ("/api/v1/credit_notes", "credit_note_id", "OUTGOING"),
    "supplier_credit_note": ("/api/v1/supplier_credit_notes", "supplier_credit_note_id", "INCOMING"),
    "payment": ("/api/v1/payments", "refund_of_payment_id", None),
}


def refund_plan(source: str, record: dict) -> dict:
    """Direction, allocation key, amount left and a plain-words label."""
    _api, key, direction = REFUND_SOURCES[source]
    if source == "payment":
        incoming = str(record.get("direction") or "").upper() == "INCOMING"
        direction = "OUTGOING" if incoming else "INCOMING"
        label = (
            "Refund money this customer paid on account"
            if incoming
            else "Record the supplier paying back money you paid on account"
        )
    elif source == "credit_note":
        label = "Refund this credit note to the customer"
    else:
        label = "Record the supplier refunding this credit to you"
    return {
        "direction": direction,
        "key": key,
        "remaining": remaining_of(record),
        "label": label,
        "money_out": direction == "OUTGOING",
    }


def business_error(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:
        return f"API error: HTTP {resp.status_code}"
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("detail") or detail.get("message") or detail.get("code") or detail)
    if isinstance(detail, list) and detail:
        first = detail[0]
        return str(first.get("msg", first)) if isinstance(first, dict) else str(first)
    if isinstance(detail, str):
        return detail
    return f"API error: HTTP {resp.status_code}"


def parse_form(side: Side, form: Any) -> tuple[list[dict], list[str]]:
    """Read ``amount__<doc id>`` fields. Blank / zero lines are skipped."""
    allocations: list[dict] = []
    errors: list[str] = []
    for key in form:
        if not str(key).startswith("amount__"):
            continue
        raw = str(form.get(key) or "").strip().replace(",", "")
        if not raw:
            continue
        doc_id = str(key)[len("amount__"):]
        try:
            uuid.UUID(doc_id)
            amount = Decimal(raw)
        except (InvalidOperation, ValueError):
            errors.append(f"'{raw}' is not an amount")
            continue
        if amount < _ZERO:
            errors.append("Amounts cannot be negative")
            continue
        if amount == _ZERO:
            continue
        if amount != amount.quantize(_CENT):
            errors.append(f"{raw} has more than 2 decimal places")
            continue
        allocations.append({side.doc_key: doc_id, "amount": str(amount)})
    return allocations, errors


async def submit_apply(request: Request, side: Side, credit_id: str) -> RedirectResponse:
    back = f"{side.credit_web}/{credit_id}"
    form = await request.form()
    version = str(form.get("version", ""))
    allocations, errors = parse_form(side, form)
    if errors:
        request.session["flash"] = "; ".join(errors)
        return RedirectResponse(url=back, status_code=303)
    if not allocations:
        request.session["flash"] = f"Enter an amount against at least one {side.doc_label}."
        return RedirectResponse(url=back, status_code=303)
    body: dict[str, Any] = {"allocations": allocations}
    if not side.is_payment:
        applied_date = str(form.get("applied_date") or "").strip()
        if applied_date:
            body["applied_date"] = applied_date
        notes = str(form.get("notes") or "").strip()
        if notes:
            body["notes"] = notes
    idem = str(form.get("idem") or "").strip() or str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"{side.credit_api}/{credit_id}/{side.apply_path}",
            json=body,
            headers={"If-Match": version, "X-Idempotency-Key": idem},
        )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code in (200, 201):
        total = sum((_dec(a["amount"]) for a in allocations), _ZERO)
        n = len(allocations)
        noun = side.doc_label if n == 1 else side.doc_label_plural
        what = "money on account" if side.is_payment else "credit"
        request.session["flash"] = f"Applied {total:,.2f} of {what} to {n} {noun}."
    elif resp.status_code == 409:
        request.session["flash"] = (
            "This credit changed since the page loaded (or that is more than is "
            "outstanding) — nothing was applied. Check the figures and try again."
        )
    else:
        request.session["flash"] = business_error(resp)
    return RedirectResponse(url=back, status_code=303)


async def submit_unapply(
    request: Request, side: Side, credit_id: str, application_id: str
) -> RedirectResponse:
    back = f"{side.credit_web}/{credit_id}"
    form = await request.form()
    version = str(form.get("version", ""))
    async with api_client(request) as client:
        resp = await client.delete(
            f"{side.credit_api}/{credit_id}/{side.apply_path}/{application_id}",
            params={} if side.is_payment else {"reason": "Un-applied in the web app"},
            headers={"If-Match": version},
        )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 200:
        request.session["flash"] = (
            f"Un-applied. The {side.doc_label} is open again for that amount "
            + ("and the money is back on account." if side.is_payment else "and the credit has it back.")
        )
    elif resp.status_code == 409:
        request.session["flash"] = "This credit changed since the page loaded — try again."
    else:
        request.session["flash"] = business_error(resp)
    return RedirectResponse(url=back, status_code=303)
