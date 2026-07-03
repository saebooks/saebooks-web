"""Bad-debt recovery detection + record — Phase 2 / Task 11.

When a customer who has a WRITTEN_OFF invoice pays money in, the company's
``recovery_mode`` decides what happens:

  * smart_prompt — show a prompt: "this payer has a written-off invoice —
    treat this receipt as a bad-debt recovery?" The operator confirms and we
    call the engine ``POST /api/v1/invoices/{id}/record-recovery`` (which posts
    Dr bank / Cr 4-1290 Bad Debt Recovery, no GST).
  * manual       — never prompt; the operator records recoveries by hand.
  * reopen        — TODO stub: re-open the original invoice instead of posting
                    a recovery. Not implemented yet; surfaced as a notice.

Detection is wired into the payment-create success path (see
``routes/payments.py``) which redirects here when a prompt is warranted. The
web app NEVER posts the journal entry itself — the engine owns the ledger.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.bad_debt_logic import WRITTEN_OFF_STATUS, should_prompt_recovery

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


async def _written_off_for_contact(client, contact_id: str) -> list[dict]:
    """Return the contact's WRITTEN_OFF invoices (empty list on error)."""
    resp = await client.get(
        "/api/v1/invoices",
        params={
            "status": WRITTEN_OFF_STATUS,
            "contact_id": contact_id,
            "page": 1,
            "page_size": 200,
        },
    )
    if resp.is_success:
        return resp.json().get("items", [])
    return []


async def detect_recovery_prompt(
    client, *, contact_id: str | None, recovery_mode: str | None
) -> list[dict]:
    """Return WRITTEN_OFF invoices to prompt on, or [] if no prompt is due.

    Encapsulates the smart_prompt gate so the payment route can ask "should I
    redirect to the recovery prompt?" with one call. Returns [] when the mode
    is not smart_prompt, no contact, or the contact has no written-off debt.
    """
    if not contact_id or not should_prompt_recovery(recovery_mode):
        return []
    return await _written_off_for_contact(client, contact_id)


@router.get("/bad-debts/recovery/prompt", response_class=HTMLResponse, response_model=None)
async def recovery_prompt(
    request: Request,
    contact_id: str = Query(...),
    amount: str = Query(...),
    bank_account_id: str = Query(...),
    payment_id: str | None = Query(default=None),
    recovery_date: str | None = Query(default=None),
) -> HTMLResponse | RedirectResponse:
    """Render the "treat as bad-debt recovery?" prompt for a payer.

    Lists the contact's WRITTEN_OFF invoices so the operator can confirm which
    one the receipt recovers (or dismiss). Reached via redirect from the
    payment-create success path in smart_prompt mode.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        written_off = await _written_off_for_contact(client, contact_id)

    # If nothing's written off (e.g. stale link), bounce to payment detail.
    if not written_off:
        target = f"/payments/{payment_id}" if payment_id else "/payments"
        return RedirectResponse(url=target, status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "bad_debts/recovery_prompt.html",
        {
            "contact_id": contact_id,
            "amount": amount,
            "bank_account_id": bank_account_id,
            "payment_id": payment_id,
            "recovery_date": recovery_date or "",
            "written_off": written_off,
        },
    )


@router.post("/bad-debts/recovery/record", response_class=HTMLResponse, response_model=None)
async def recovery_record(request: Request) -> HTMLResponse | RedirectResponse:
    """Record a bad-debt recovery against a chosen WRITTEN_OFF invoice.

    Proxies to engine ``POST /api/v1/invoices/{id}/record-recovery``. On
    success, flash + redirect to the invoice. On 409/404/422, redirect back to
    the prompt with an error flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    invoice_id = (form.get("invoice_id") or "").strip()
    bank_account_id = (form.get("bank_account_id") or "").strip()
    amount = (form.get("amount") or "").strip()
    payer_contact_id = (form.get("contact_id") or "").strip() or None
    recovery_date = (form.get("recovery_date") or "").strip() or None

    if not (invoice_id and bank_account_id and amount):
        request.session["flash"] = "Recovery needs an invoice, bank account, and amount."
        return RedirectResponse(url="/payments", status_code=303)

    payload: dict[str, object] = {
        "bank_account_id": bank_account_id,
        "amount": amount,
    }
    if recovery_date:
        payload["recovery_date"] = recovery_date
    if payer_contact_id:
        payload["payer_contact_id"] = payer_contact_id

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/record-recovery",
            json=payload,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        request.session["flash"] = (
            f"Recorded ${amount} as a bad-debt recovery (posted to Bad Debt Recovery income)."
        )
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    msg = f"Recovery failed (HTTP {resp.status_code})"
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, str):
            msg = detail
    except Exception:
        pass
    request.session["flash"] = msg
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)
