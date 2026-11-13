"""Getting paid without chasing — web screens (b11).

* ``/settings/getting-paid``          reminders, sending address, online
                                      payment accounts, scheduler health,
                                      "Run today's jobs now"
* ``/statements/send``                month-end bulk customer statements
* ``POST /invoices/{id}/send-reminder``  "Send reminder" (invoice page and
                                      overdue rows)
* ``POST /contacts/{id}/statement/email``  email one statement (period picker)
* ``POST /contacts/{id}/reminders``   turn automatic reminders off/on for a
                                      customer
* ``POST /credit-notes/{id}/email``   email a posted credit note
* ``GET/POST /pay/{token}``           the PUBLIC pay page a customer lands on
                                      from the "Pay now" link (no login)

Everything is a thin proxy over the engine (``/api/v1/collections``,
``/api/v1/invoices/{id}/send-reminder``, ``/api/v1/pay/{token}`` ...).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.config import settings

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _authed(request: Request) -> bool:
    return bool(request.session.get("api_token"))


def _msg(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        return f"HTTP {resp.status_code}"
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        return str(detail[0].get("msg") or detail[0])
    return f"HTTP {resp.status_code}"


def _back(request: Request, fallback: str) -> str:
    """Same-site Referer path, else ``fallback`` (never an open redirect)."""
    ref = request.headers.get("referer") or ""
    if ref:
        try:
            u = httpx.URL(ref)
            if u.host == request.url.hostname and u.path.startswith("/"):
                return u.path + (f"?{u.query.decode()}" if u.query else "")
        except Exception:
            pass
    return fallback


_OUTCOME_WORDS = {
    "sent": "sent",
    "drafted": "saved as a draft in Outlook for you to send",
    "blocked": "not sent: outgoing email is switched off",
    "failed": "could not be sent",
    "skipped": "not sent",
}


# --------------------------------------------------------------------------- #
# Settings + health                                                            #
# --------------------------------------------------------------------------- #


@router.get("/settings/getting-paid", response_class=HTMLResponse, response_model=None)
async def getting_paid_settings(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        s = await client.get("/api/v1/collections/settings")
        if s.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        st = await client.get("/api/v1/collections/status")
        acc = await client.get("/api/v1/accounts", params={"limit": 500, "offset": 0})
        tax = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    accounts = acc.json().get("items", []) if acc.is_success else []
    return _TEMPLATES.TemplateResponse(
        request,
        "getting_paid/settings.html",
        {
            "s": s.json() if s.is_success else {},
            "status": st.json() if st.is_success else {},
            "error": None if s.is_success else _msg(s),
            "money_accounts": [
                a for a in accounts
                if (a.get("account_type") or "").upper() in ("ASSET", "BANK", "CURRENT_ASSET")
            ] or accounts,
            "expense_accounts": [
                a for a in accounts if (a.get("account_type") or "").upper() in ("EXPENSE", "COST_OF_SALES")
            ] or accounts,
            "tax_codes": tax.json().get("items", []) if tax.is_success else [],
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/settings/getting-paid", response_model=None)
async def getting_paid_settings_save(request: Request) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body: dict[str, Any] = {
        "reminders_enabled": bool(form.get("reminders_enabled")),
        "reminder_days": str(form.get("reminder_days") or ""),
        "from_addr": (str(form.get("from_addr") or "").strip() or None),
        "reminder_message": (str(form.get("reminder_message") or "").strip() or None),
        "online_payment_account_id": (str(form.get("online_payment_account_id") or "") or None),
        "online_fee_account_id": (str(form.get("online_fee_account_id") or "") or None),
        "online_fee_tax_code_id": (str(form.get("online_fee_tax_code_id") or "") or None),
    }
    async with api_client(request) as client:
        r = await client.put("/api/v1/collections/settings", json=body)
    request.session["flash"] = "Saved." if r.is_success else f"Not saved: {_msg(r)}"
    return RedirectResponse(url="/settings/getting-paid", status_code=303)


@router.post("/settings/getting-paid/run", response_model=None)
async def getting_paid_run_now(request: Request) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        r = await client.post("/api/v1/collections/run", timeout=120.0)
    if r.is_success:
        out = r.json()
        rem = out.get("reminders") or {}
        bits = [
            f"{out.get('invoices_made', 0)} invoice(s) made",
            f"{out.get('invoices_sent', 0)} emailed",
            f"{out.get('bills_made', 0)} bill(s) made",
            f"{rem.get('sent', 0)} reminder(s) sent",
        ]
        errs = out.get("errors") or []
        request.session["flash"] = "Done: " + ", ".join(bits) + (
            ". Problems: " + "; ".join(errs) if errs else "."
        )
    else:
        request.session["flash"] = f"Could not run: {_msg(r)}"
    return RedirectResponse(url="/settings/getting-paid", status_code=303)


# --------------------------------------------------------------------------- #
# Reminders                                                                    #
# --------------------------------------------------------------------------- #


@router.post("/invoices/{invoice_id}/send-reminder", response_model=None)
async def invoice_send_reminder(request: Request, invoice_id: str) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/invoices/{invoice_id}/send-reminder", json={}, timeout=60.0)
    if r.is_success:
        out = r.json()
        words = _OUTCOME_WORDS.get(out.get("outcome", ""), out.get("outcome", ""))
        to = ", ".join(out.get("to") or [])
        request.session["flash"] = f"Reminder {words}" + (f" to {to}" if to and out.get("outcome") == "sent" else "") + (
            f" ({out['detail']})" if out.get("detail") and out.get("outcome") != "sent" else ""
        ) + "."
    else:
        request.session["flash"] = f"Reminder not sent: {_msg(r)}"
    return RedirectResponse(url=_back(request, f"/invoices/{invoice_id}"), status_code=303)


@router.post("/contacts/{contact_id}/reminders", response_model=None)
async def contact_reminders_toggle(request: Request, contact_id: str) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    opt_out = form.get("opt_out") == "1"
    async with api_client(request) as client:
        c = await client.get(f"/api/v1/contacts/{contact_id}")
        if not c.is_success:
            request.session["flash"] = f"Not changed: {_msg(c)}"
            return RedirectResponse(url=f"/contacts/{contact_id}", status_code=303)
        r = await client.patch(
            f"/api/v1/contacts/{contact_id}",
            json={"reminders_opt_out": opt_out},
            headers={"If-Match": str(c.json().get("version", ""))},
        )
    if r.is_success:
        request.session["flash"] = (
            "Automatic payment reminders are off for this customer."
            if opt_out else "Automatic payment reminders are on for this customer."
        )
    else:
        request.session["flash"] = f"Not changed: {_msg(r)}"
    return RedirectResponse(url=f"/contacts/{contact_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Statements                                                                   #
# --------------------------------------------------------------------------- #


def _period(request: Request, form: Any | None = None) -> tuple[str, str]:
    src = form if form is not None else request.query_params
    today = date.today()
    first_this_month = today.replace(day=1)
    default_to = first_this_month - timedelta(days=1)
    default_from = default_to.replace(day=1)
    pf = str(src.get("from") or default_from.isoformat())
    pt = str(src.get("to") or default_to.isoformat())
    return pf, pt


@router.post("/contacts/{contact_id}/statement/email", response_model=None)
async def contact_statement_email(request: Request, contact_id: str) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    pf, pt = _period(request, form)
    body: dict[str, Any] = {"from": pf, "to": pt}
    if form.get("recipients"):
        body["recipients"] = str(form.get("recipients"))
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/contacts/{contact_id}/statement/send-email", json=body, timeout=60.0)
    if r.is_success:
        out = r.json()
        request.session["flash"] = f"Statement {_OUTCOME_WORDS.get(out.get('mode'), out.get('mode'))}" + (
            f" ({out['reason']})" if out.get("reason") and out.get("mode") != "sent" else ""
        ) + "."
    else:
        request.session["flash"] = f"Statement not sent: {_msg(r)}"
    return RedirectResponse(url=f"/contacts/{contact_id}", status_code=303)


@router.get("/statements/send", response_class=HTMLResponse, response_model=None)
async def bulk_statements_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    pf, pt = _period(request)
    async with api_client(request) as client:
        r = await client.get("/api/v1/collections/statement-candidates", params={"as_of": pt})
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "getting_paid/statements_send.html",
        {
            "period_from": pf,
            "period_to": pt,
            "items": r.json().get("items", []) if r.is_success else [],
            "error": None if r.is_success else _msg(r),
            "results": request.session.pop("bulk_statement_results", None),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/statements/send", response_model=None)
async def bulk_statements_send(request: Request) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    pf, pt = _period(request, form)
    ids = [str(v) for v in form.getlist("contact_id") if v]
    if not ids:
        request.session["flash"] = "Tick at least one customer."
        return RedirectResponse(url=f"/statements/send?from={pf}&to={pt}", status_code=303)
    async with api_client(request) as client:
        r = await client.post(
            "/api/v1/collections/statements/send-bulk",
            json={"period_from": pf, "period_to": pt, "contact_ids": ids},
            timeout=300.0,
        )
    if r.is_success:
        out = r.json()
        c = out.get("counts", {})
        request.session["flash"] = (
            f"{c.get('sent', 0)} sent, {c.get('drafted', 0)} saved as drafts, "
            f"{c.get('blocked', 0) + c.get('failed', 0) + c.get('skipped', 0)} not sent."
        )
        # Keep only what the page shows (session cookie size).
        request.session["bulk_statement_results"] = [
            {k: x.get(k) for k in ("contact_id", "name", "outcome", "reason")}
            for x in out.get("results", [])
            if x.get("outcome") not in ("sent", "drafted")
        ][:50]
    else:
        request.session["flash"] = f"Not sent: {_msg(r)}"
    return RedirectResponse(url=f"/statements/send?from={pf}&to={pt}", status_code=303)


# --------------------------------------------------------------------------- #
# Credit notes                                                                 #
# --------------------------------------------------------------------------- #


@router.post("/credit-notes/{credit_note_id}/email", response_model=None)
async def credit_note_email(request: Request, credit_note_id: str) -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body: dict[str, Any] = {}
    if form.get("to"):
        body["to"] = str(form.get("to"))
    async with api_client(request) as client:
        r = await client.post(f"/api/v1/credit_notes/{credit_note_id}/send-email", json=body, timeout=60.0)
    if r.is_success:
        out = r.json()
        request.session["flash"] = f"Credit note {_OUTCOME_WORDS.get(out.get('mode'), out.get('mode'))}" + (
            f" ({out['reason']})" if out.get("reason") and out.get("mode") != "sent" else ""
        ) + "."
    else:
        request.session["flash"] = f"Credit note not sent: {_msg(r)}"
    return RedirectResponse(url=f"/credit-notes/{credit_note_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Public pay page — NO login                                                   #
# --------------------------------------------------------------------------- #


def _engine() -> httpx.AsyncClient:
    """Anonymous engine client: the pay token is the only credential."""
    return httpx.AsyncClient(base_url=settings.api_url, timeout=20.0)


def _fmt_money(value: str | None, currency: str | None) -> str:
    try:
        amount = Decimal(str(value or "0"))
    except InvalidOperation:
        amount = Decimal("0")
    cur = (currency or "AUD").upper()
    return f"${amount:,.2f}" if cur == "AUD" else f"{cur} {amount:,.2f}"


@router.get("/pay/{token}", response_class=HTMLResponse, response_model=None)
async def pay_page(request: Request, token: str) -> HTMLResponse:
    summary: dict[str, Any] | None = None
    error = None
    try:
        async with _engine() as client:
            r = await client.get(f"/api/v1/pay/{token}")
        if r.is_success:
            summary = r.json()
        elif r.status_code == 404:
            error = "This payment link is not valid. Please check it, or contact the business that sent it."
        else:
            error = "This page is not available right now. Please try again shortly."
    except httpx.HTTPError:
        error = "This page is not available right now. Please try again shortly."
    return _TEMPLATES.TemplateResponse(
        request,
        "getting_paid/pay.html",
        {
            "token": token,
            "s": summary,
            "error": error,
            "paid_return": request.query_params.get("paid") == "1",
            "balance": _fmt_money(summary.get("balance_due"), summary.get("currency")) if summary else "",
            "total": _fmt_money(summary.get("total"), summary.get("currency")) if summary else "",
        },
        status_code=404 if (error and summary is None and "not valid" in error) else 200,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


@router.post("/pay/{token}", response_class=HTMLResponse, response_model=None)
async def pay_go(request: Request, token: str) -> HTMLResponse | RedirectResponse:
    try:
        async with _engine() as client:
            r = await client.post(f"/api/v1/pay/{token}/checkout")
    except httpx.HTTPError:
        r = None
    if r is not None and r.is_success and str(r.json().get("url", "")).startswith("https://"):
        return RedirectResponse(url=r.json()["url"], status_code=303)
    message = (
        _msg(r) if r is not None and r.status_code == 422
        else "The card payment page could not be opened just now. Please try again shortly."
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "getting_paid/pay.html",
        {"token": token, "s": None, "error": message, "paid_return": False, "balance": "", "total": ""},
        status_code=200,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )
