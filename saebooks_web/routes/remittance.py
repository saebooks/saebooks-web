"""Remittance advice from the payment page (batch 3, gap P0-13).

GET  /payments/{id}/remittance.pdf          view inline (``?download=1`` saves)
GET  /payments/{id}/remittance/email        compose: To = the supplier's email
POST /payments/{id}/remittance/email        send through the engine

The engine renders the PDF (``GET /api/v1/payments/{id}/remittance.pdf``) and
sends it (``POST /api/v1/payments/{id}/remittance/send-email``) behind the
same two-key kill switch as invoices: a blocked send comes back HTTP 200 with
``mode="blocked"``, so the outcome is read from the body, never the status.

The remittance prints the payment's "message to the supplier", never its
internal notes; drafts and voided payments are refused by the engine and the
refusal is shown as-is.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.settlement import payment_settlement

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _from_options(company: dict[str, Any]) -> tuple[list[str], str]:
    """Sender choices: the configured outbound addresses plus the company's
    own email. The engine's allowlist has the final say."""
    options: list[str] = []
    default = ""
    try:
        from saebooks_web.routes import quotes as _quotes

        options.extend(getattr(_quotes, "_FROM_OPTIONS", ()))
        default = getattr(_quotes, "_DEFAULT_FROM_BY_DOC_TYPE", {}).get("remittance", "")
    except Exception:  # pragma: no cover - defensive
        pass
    company_email = str(company.get("email") or "").strip()
    if company_email and company_email not in options:
        options.append(company_email)
    if not default:
        default = company_email or (options[0] if options else "")
    return options, default


def _money(value: Any, currency: str) -> str:
    try:
        text = f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        text = str(value)
    return f"${text}" if (currency or "AUD").upper() == "AUD" else f"{currency} {text}"


def default_email_fields(
    payment: dict[str, Any], supplier: dict[str, Any], company: dict[str, Any],
    settlement: dict[str, Any] | None,
) -> dict[str, str]:
    """Plain default To / Subject / Body for a remittance email."""
    number = payment.get("number") or str(payment.get("id", ""))[:8]
    currency = payment.get("currency") or "AUD"
    company_name = company.get("legal_name") or company.get("name") or ""
    refs: list[str] = []
    for a in (settlement or {}).get("allocations", []):
        ref = a.get("supplier_reference") or a.get("number")
        if ref and a.get("kind") == "bill":
            refs.append(str(ref))
    paid_for = f" for invoice{'s' if len(refs) > 1 else ''} {', '.join(refs)}" if refs else ""
    greeting = supplier.get("name") or "there"
    body = (
        f"<p>Hello {html.escape(str(greeting))},</p>\n"
        f"<p>We have paid {html.escape(_money(payment.get('amount'), currency))}"
        f"{html.escape(paid_for)} on {html.escape(str(payment.get('payment_date') or ''))}. "
        f"The remittance advice is attached.</p>\n"
        f"<p>Kind regards,<br>{html.escape(company_name)}</p>"
    )
    subject = f"Remittance advice {number}"
    if company_name:
        subject += f" from {company_name}"
    return {"to": str(supplier.get("email") or ""), "subject": subject, "body_html": body}


async def _load(client: Any, payment_id: str) -> tuple[Any, dict, dict, dict, dict | None]:
    resp = await client.get(f"/api/v1/payments/{payment_id}")
    if not resp.is_success:
        return resp, {}, {}, {}, None
    payment = resp.json()
    supplier: dict[str, Any] = {}
    if payment.get("contact_id"):
        c = await client.get(f"/api/v1/contacts/{payment['contact_id']}")
        supplier = c.json() if c.is_success else {}
    company: dict[str, Any] = {}
    co = await client.get(f"/api/v1/companies/{payment['company_id']}") if payment.get("company_id") else None
    if co is not None and co.is_success:
        company = co.json()
    else:
        co = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if co.is_success:
            items = co.json().get("items", [])
            company = items[0] if items else {}
    settlement = await payment_settlement(client, payment_id)
    return resp, payment, supplier, company, settlement


@router.get("/payments/{payment_id}/remittance.pdf", response_model=None)
async def remittance_pdf(
    request: Request, payment_id: str, download: int = 0
) -> Response | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/payments/{payment_id}/remittance.pdf")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        request.session["flash"] = (
            f"The remittance advice could not be made: {detail}"
            if detail else f"The remittance advice could not be made (HTTP {resp.status_code})."
        )
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)
    disposition = resp.headers.get(
        "content-disposition", f'inline; filename="remittance-{payment_id}.pdf"'
    )
    if download:
        disposition = disposition.replace("inline", "attachment", 1)
    return Response(
        content=resp.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


def _render(request: Request, ctx: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "payments/remittance_email.html", ctx, status_code=status_code
    )


@router.get("/payments/{payment_id}/remittance/email", response_class=HTMLResponse, response_model=None)
async def remittance_email_compose(request: Request, payment_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp, payment, supplier, company, settlement = await _load(client, payment_id)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        return HTMLResponse("Payment not found", status_code=404)
    if payment.get("direction") != "OUTGOING" or payment.get("status") != "POSTED":
        request.session["flash"] = (
            "A remittance advice can be emailed once a payment to a supplier is posted."
        )
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)
    from_options, default_from = _from_options(company)
    defaults = default_email_fields(payment, supplier, company, settlement)
    return _render(request, {
        "payment": payment, "supplier": supplier, "settlement": settlement,
        "form": {"from_addr": default_from, **defaults},
        "from_options": from_options, "flash": None, "flash_kind": None,
        "no_email": not defaults["to"],
    })


def outcome_message(resp: Any) -> tuple[str, str]:
    """(message, kind) for a send-email response — read ``mode`` from the
    body; a blocked send is HTTP 200."""
    try:
        result = resp.json()
    except Exception:
        result = {}
    if not isinstance(result, dict):
        result = {}
    mode = result.get("mode")
    if resp.is_success and mode == "sent":
        return "Remittance advice emailed to the supplier.", "pos"
    if resp.is_success and mode == "blocked":
        reason = result.get("reason") or "outbound email is switched off"
        return (
            f"Not sent — outbound email is switched off for this business ({reason}). "
            "A copy was saved to the outbox; nothing left the server.",
            "warm",
        )
    if resp.is_success and mode == "draft":
        return "Saved as a draft email — not sent.", "warm"
    detail = result.get("detail") or result.get("errors") or result.get("reason")
    return (
        f"The remittance advice was not sent: {detail}" if detail
        else f"The remittance advice was not sent (HTTP {resp.status_code}).",
        "neg",
    )


@router.post("/payments/{payment_id}/remittance/email", response_class=HTMLResponse, response_model=None)
async def remittance_email_send(request: Request, payment_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form_data = await request.form()
    form = {k: str(v) for k, v in form_data.items() if k != "csrf_token"}
    split = lambda s: [x.strip() for x in (s or "").replace(";", ",").split(",") if x.strip()]  # noqa: E731
    payload = {
        "from_addr": form.get("from_addr", ""),
        "to": split(form.get("to")),
        "cc": split(form.get("cc")),
        "bcc": split(form.get("bcc")),
        "subject": form.get("subject", ""),
        "body_html": form.get("body_html", ""),
        "sent_by_user_id": request.session.get("user_id"),
    }
    async with api_client(request) as client:
        if not payload["to"]:
            resp = None
        else:
            resp = await client.post(
                f"/api/v1/payments/{payment_id}/remittance/send-email", json=payload
            )
        if resp is not None and resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp is not None:
            message, kind = outcome_message(resp)
            if kind in ("pos", "warm"):
                request.session["flash"] = message
                return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)
        else:
            message, kind = "Enter the supplier's email address.", "neg"
        _resp, payment, supplier, company, settlement = await _load(client, payment_id)
    from_options, _ = _from_options(company)
    return _render(request, {
        "payment": payment, "supplier": supplier, "settlement": settlement,
        "form": form, "from_options": from_options,
        "flash": message, "flash_kind": kind, "no_email": False,
    }, status_code=422)


async def email_with_defaults(request: Request, client: Any, payment_id: str) -> str:
    """Send the remittance with the default wording (the "email the
    remittance once posted" option). Returns the flash message."""
    resp, payment, supplier, company, settlement = await _load(client, payment_id)
    if not resp.is_success:
        return "The remittance advice was not sent: the payment could not be read."
    defaults = default_email_fields(payment, supplier, company, settlement)
    if not defaults["to"]:
        return (
            "The remittance advice was not sent: this supplier has no email address. "
            "Add one on the supplier's page, then use Email remittance."
        )
    _, default_from = _from_options(company)
    send = await client.post(
        f"/api/v1/payments/{payment_id}/remittance/send-email",
        json={
            "from_addr": default_from, "to": [defaults["to"]], "cc": [], "bcc": [],
            "subject": defaults["subject"], "body_html": defaults["body_html"],
            "sent_by_user_id": request.session.get("user_id"),
        },
    )
    return outcome_message(send)[0]
