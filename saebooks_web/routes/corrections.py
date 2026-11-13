"""Fixing mistakes, document history and approvals (batch 13).

Screens:

GET  /invoices/{id}/fix           "Fix this invoice" -- every correction in one
GET  /bills/{id}/fix              place, each with a plain explanation of what it
                                  does to the books.
POST /invoices/{id}/correct       Posted invoice: due date, payment terms, notes
POST /bills/{id}/correct          Posted bill: due date, supplier's reference, notes
POST /invoices/{id}/duplicate     Copy to a new draft (also the list's button)
POST /bills/{id}/duplicate
POST /invoices/{id}/credit-and-reissue
POST /invoices/{id}/void-and-copy
POST /bills/{id}/void-and-copy
POST /invoices/{id}/void-with-reason   void with a reason (and, for an admin,
POST /bills/{id}/void-with-reason      a reason for changing a locked period)
POST /invoices/{id}/change-contact
POST /bills/{id}/change-contact
POST /invoices/{id}/lines/{line_id}/change-account
POST /bills/{id}/lines/{line_id}/change-account
POST /invoices/{id}/write-off     write off as a bad debt, from the invoice
GET  /history/{entity}/{id}       the History panel (HTMX partial)
POST /bills/{id}/approval         send for approval / approve / send back
POST /payments/{id}/approval
GET  /approvals                   the sign-off list + company switches
POST /approvals/settings

The engine owns every ledger effect; this module only drives the screens.
Errors from the engine (including a locked period) come back as a flash
message in plain words.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Roles that may change a locked period (engine: journal._OVERRIDE_ROLES).
OVERRIDE_ROLES = frozenset({"admin", "accountant", "owner"})

_HISTORY_ENTITIES = frozenset({
    "invoice", "bill", "payment", "credit_note", "supplier_credit_note",
    "expense", "receipt", "quote", "purchase_order", "journal_entry",
})


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def can_override(request: Request) -> bool:
    return (request.session.get("user_role") or "") in OVERRIDE_ROLES


def _error_text(resp: Any) -> str:
    """The engine's message in plain words, whatever shape it came in."""
    try:
        body = resp.json()
    except Exception:
        return f"Something went wrong (error {resp.status_code})."
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("detail") or detail.get("message") or detail.get("code")
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            loc = first.get("loc") or []
            field = str(loc[-1]).replace("_", " ") if loc else ""
            msg = str(first.get("msg", ""))
            if "at least 1 character" in msg or msg.lower().startswith("field required"):
                return f"Please fill in the {field}." if field else "Please fill in every field."
            return f"{field}: {msg}" if field else msg
        return str(first)
    if isinstance(detail, str) and detail:
        if detail == "version mismatch":
            return "Someone else changed this while you were looking at it. Please try again."
        return detail
    return f"Something went wrong (error {resp.status_code})."


_ZONES = {"AU": "Australia/Brisbane", "EE": "Europe/Tallinn", "NZ": "Pacific/Auckland",
          "GB": "Europe/London", "UK": "Europe/London"}


def _local_zone(request: Request) -> Any:
    from zoneinfo import ZoneInfo

    j = (getattr(request.state, "active_company_jurisdiction", None) or "AU").upper()
    try:
        return ZoneInfo(_ZONES.get(j, "UTC"))
    except Exception:  # pragma: no cover - tzdata missing
        return None


def _local_when(at: str | None, tz: Any) -> str:
    """'13 Nov 2026, 2:05 pm' in the company's time zone."""
    if not at:
        return ""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return at
    if tz is not None and dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    hour = dt.hour % 12 or 12
    return f"{dt.day} {dt:%b %Y}, {hour}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"


def _form_str(form: Any, key: str) -> str:
    return str(form.get(key, "") or "").strip()


def _back(url: str, request: Request, message: str) -> RedirectResponse:
    request.session["flash"] = message
    return RedirectResponse(url=url, status_code=303)


async def _load(client: Any, kind: str, doc_id: str) -> tuple[int, dict | None]:
    path = "invoices" if kind == "invoice" else "bills"
    resp = await client.get(f"/api/v1/{path}/{doc_id}")
    if resp.is_success:
        return resp.status_code, resp.json()
    return resp.status_code, None


async def _contacts(client: Any, kind: str) -> list[dict]:
    types = ("CUSTOMER", "BOTH") if kind == "invoice" else ("SUPPLIER", "BOTH")
    out: list[dict] = []
    for ctype in types:
        r = await client.get("/api/v1/contacts", params={"type": ctype, "limit": 500, "offset": 0})
        if r.is_success:
            out.extend(r.json().get("items", []))
    out.sort(key=lambda c: (c.get("name") or "").lower())
    return out


async def _accounts(client: Any) -> list[dict]:
    r = await client.get("/api/v1/accounts", params={"limit": 1000, "offset": 0})
    if not r.is_success:
        return []
    items = [a for a in r.json().get("items", []) if not a.get("is_header") and not a.get("archived_at")]
    items.sort(key=lambda a: a.get("code", ""))
    return items


# ---------------------------------------------------------------------------
# Fix page
# ---------------------------------------------------------------------------


async def _fix_page(request: Request, kind: str, doc_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    async with api_client(request) as client:
        status, doc = await _load(client, kind, doc_id)
        if status == 401:
            request.session.clear()
            return _login()
        if doc is None:
            return _back(f"/{base}", request, "That document was not found.")
        contacts = await _contacts(client, kind)
        accounts = await _accounts(client)
        line_state: dict[str, Any] = {"lines": [], "reclassifications": []}
        if doc.get("status") == "POSTED":
            r = await client.get(f"/api/v1/{base}/{doc_id}/line-accounts")
            if r.is_success:
                line_state = r.json()
    names = {a["id"]: f'{a.get("code", "")} {a.get("name", "")}'.strip() for a in accounts}
    current = {x["line_id"]: x["current_account_id"] for x in line_state.get("lines", [])}
    lines = []
    for ln in sorted(doc.get("lines", []), key=lambda x: x.get("line_no", 0)):
        acct_now = current.get(ln["id"], ln.get("account_id"))
        lines.append({
            **ln,
            "current_account_id": acct_now,
            "current_account_name": names.get(acct_now, ""),
            "moved": acct_now != ln.get("account_id"),
        })
    contact_name = next((c.get("name") for c in contacts if c.get("id") == doc.get("contact_id")), None)
    outstanding = float(doc.get("total") or 0) - float(doc.get("amount_paid") or 0)
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "corrections/fix.html",
        {
            "kind": kind,
            "base": base,
            "doc": doc,
            "contacts": contacts,
            "contact_name": contact_name,
            "accounts": accounts,
            "lines": lines,
            "outstanding": outstanding,
            "can_override": can_override(request),
            "today": date.today().isoformat(),
            "flash": flash,
        },
    )


@router.get("/invoices/{invoice_id}/fix", response_class=HTMLResponse, response_model=None)
async def invoice_fix(request: Request, invoice_id: str) -> HTMLResponse | RedirectResponse:
    return await _fix_page(request, "invoice", invoice_id)


@router.get("/bills/{bill_id}/fix", response_class=HTMLResponse, response_model=None)
async def bill_fix(request: Request, bill_id: str) -> HTMLResponse | RedirectResponse:
    return await _fix_page(request, "bill", bill_id)


# ---------------------------------------------------------------------------
# Correct details on a posted document (no ledger effect)
# ---------------------------------------------------------------------------


async def _correct(request: Request, kind: str, doc_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    form = await request.form()
    body: dict[str, Any] = {}
    due = _form_str(form, "due_date")
    if due:
        body["due_date"] = due
    if "notes" in form:
        body["notes"] = _form_str(form, "notes")
    if kind == "invoice" and "payment_terms" in form:
        body["payment_terms"] = _form_str(form, "payment_terms")
    if kind == "bill" and "supplier_reference" in form:
        body["supplier_reference"] = _form_str(form, "supplier_reference")
    version = _form_str(form, "version")
    async with api_client(request) as client:
        resp = await client.patch(f"/api/v1/{base}/{doc_id}", json=body, headers={"If-Match": version})
        if resp.status_code == 401:
            request.session.clear()
            return _login()
        if kind == "bill" and resp.is_success and "planned_pay_date" in form:
            new_version = str(resp.json().get("version", version))
            ppd = _form_str(form, "planned_pay_date") or None
            if ppd != (resp.json().get("planned_pay_date") or None):
                r2 = await client.put(
                    f"/api/v1/bills/{doc_id}/planned-pay-date",
                    json={"planned_pay_date": ppd},
                    headers={"If-Match": new_version},
                )
                if not r2.is_success:
                    return _back(f"/bills/{doc_id}/fix", request, _error_text(r2))
    if resp.is_success:
        return _back(f"/{base}/{doc_id}", request, "Details saved.")
    return _back(f"/{base}/{doc_id}/fix", request, _error_text(resp))


@router.post("/invoices/{invoice_id}/correct", response_model=None)
async def invoice_correct(request: Request, invoice_id: str) -> RedirectResponse:
    return await _correct(request, "invoice", invoice_id)


@router.post("/bills/{bill_id}/correct", response_model=None)
async def bill_correct(request: Request, bill_id: str) -> RedirectResponse:
    return await _correct(request, "bill", bill_id)


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------


async def _duplicate(request: Request, kind: str, doc_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/{base}/{doc_id}/duplicate", json={})
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.status_code == 201:
        new_id = resp.json()["id"]
        what = "invoice" if kind == "invoice" else "bill"
        return _back(f"/{base}/{new_id}/edit", request,
                     f"Copied to a new draft {what}. Check it, then save.")
    return _back(f"/{base}/{doc_id}", request, _error_text(resp))


@router.post("/invoices/{invoice_id}/duplicate", response_model=None)
async def invoice_duplicate(request: Request, invoice_id: str) -> RedirectResponse:
    return await _duplicate(request, "invoice", invoice_id)


@router.post("/bills/{bill_id}/duplicate", response_model=None)
async def bill_duplicate(request: Request, bill_id: str) -> RedirectResponse:
    return await _duplicate(request, "bill", bill_id)


# ---------------------------------------------------------------------------
# Credit and reissue, void and copy, void with a reason
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/credit-and-reissue", response_model=None)
async def invoice_credit_and_reissue(request: Request, invoice_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    form = await request.form()
    body: dict[str, Any] = {"reason": _form_str(form, "reason")}
    if _form_str(form, "credit_date"):
        body["credit_date"] = _form_str(form, "credit_date")
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/credit-and-reissue",
            json=body,
            headers={"If-Match": _form_str(form, "version")},
        )
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.status_code == 201:
        out = resp.json()
        cn_no = out["credit_note"].get("number") or "a credit note"
        return _back(
            f"/invoices/{out['invoice']['id']}/edit",
            request,
            f"The old invoice is credited in full by {cn_no}. This is the new draft: "
            "correct it, then post and send it.",
        )
    return _back(f"/invoices/{invoice_id}/fix", request, _error_text(resp))


def _void_body(form: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"reason": _form_str(form, "reason")}
    if _form_str(form, "override_reason"):
        body["override_reason"] = _form_str(form, "override_reason")
    return body


async def _void_and_copy(request: Request, kind: str, doc_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    form = await request.form()
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/{base}/{doc_id}/void-and-copy",
            json=_void_body(form),
            headers={"If-Match": _form_str(form, "version")},
        )
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.status_code == 201:
        new = resp.json()["invoice" if kind == "invoice" else "bill"]
        return _back(f"/{base}/{new['id']}/edit", request,
                     "The old one is voided. This is the new draft: correct it, then post it.")
    return _back(f"/{base}/{doc_id}/fix", request, _error_text(resp))


@router.post("/invoices/{invoice_id}/void-and-copy", response_model=None)
async def invoice_void_and_copy(request: Request, invoice_id: str) -> RedirectResponse:
    return await _void_and_copy(request, "invoice", invoice_id)


@router.post("/bills/{bill_id}/void-and-copy", response_model=None)
async def bill_void_and_copy(request: Request, bill_id: str) -> RedirectResponse:
    return await _void_and_copy(request, "bill", bill_id)


async def _void_with_reason(request: Request, kind: str, doc_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    form = await request.form()
    body = _void_body(form)
    if not body["reason"]:
        return _back(f"/{base}/{doc_id}/fix", request, "Please give a reason for voiding.")
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/{base}/{doc_id}/void",
            json=body,
            headers={"If-Match": _form_str(form, "version")},
        )
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.status_code in (200, 204):
        return _back(f"/{base}/{doc_id}", request, "Voided. The reason is kept in its history.")
    return _back(f"/{base}/{doc_id}/fix", request, _error_text(resp))


@router.post("/invoices/{invoice_id}/void-with-reason", response_model=None)
async def invoice_void_with_reason(request: Request, invoice_id: str) -> RedirectResponse:
    return await _void_with_reason(request, "invoice", invoice_id)


@router.post("/bills/{bill_id}/void-with-reason", response_model=None)
async def bill_void_with_reason(request: Request, bill_id: str) -> RedirectResponse:
    return await _void_with_reason(request, "bill", bill_id)


# ---------------------------------------------------------------------------
# Change contact, change a line's account
# ---------------------------------------------------------------------------


async def _change_contact(request: Request, kind: str, doc_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    form = await request.form()
    contact_id = _form_str(form, "contact_id")
    if not contact_id:
        return _back(f"/{base}/{doc_id}/fix", request, "Please choose the right contact.")
    body = {**_void_body(form), "contact_id": contact_id}
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/{base}/{doc_id}/change-contact",
            json=body,
            headers={"If-Match": _form_str(form, "version")},
        )
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.is_success:
        return _back(f"/{base}/{doc_id}", request, "Moved to the new contact.")
    return _back(f"/{base}/{doc_id}/fix", request, _error_text(resp))


@router.post("/invoices/{invoice_id}/change-contact", response_model=None)
async def invoice_change_contact(request: Request, invoice_id: str) -> RedirectResponse:
    return await _change_contact(request, "invoice", invoice_id)


@router.post("/bills/{bill_id}/change-contact", response_model=None)
async def bill_change_contact(request: Request, bill_id: str) -> RedirectResponse:
    return await _change_contact(request, "bill", bill_id)


async def _change_account(request: Request, kind: str, doc_id: str, line_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "invoices" if kind == "invoice" else "bills"
    form = await request.form()
    to_account = _form_str(form, "to_account_id")
    if not to_account:
        return _back(f"/{base}/{doc_id}/fix", request, "Please choose the account it belongs in.")
    body: dict[str, Any] = {**_void_body(form), "to_account_id": to_account}
    if _form_str(form, "reclass_date"):
        body["reclass_date"] = _form_str(form, "reclass_date")
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/{base}/{doc_id}/lines/{line_id}/reclassify", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.status_code == 201:
        return _back(f"/{base}/{doc_id}/fix", request,
                     "Line moved to the new account. The original entry is kept as it was.")
    return _back(f"/{base}/{doc_id}/fix", request, _error_text(resp))


@router.post("/invoices/{invoice_id}/lines/{line_id}/change-account", response_model=None)
async def invoice_change_account(request: Request, invoice_id: str, line_id: str) -> RedirectResponse:
    return await _change_account(request, "invoice", invoice_id, line_id)


@router.post("/bills/{bill_id}/lines/{line_id}/change-account", response_model=None)
async def bill_change_account(request: Request, bill_id: str, line_id: str) -> RedirectResponse:
    return await _change_account(request, "bill", bill_id, line_id)


# ---------------------------------------------------------------------------
# Write off from the invoice (P1-9)
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/write-off", response_model=None)
async def invoice_write_off(request: Request, invoice_id: str) -> RedirectResponse:
    """Write off what is still owing on ANY posted invoice -- not only the
    ones old enough to appear on the bad debts screen, and in every
    write-off mode (a customer who went bust today can be written off
    today). The engine posts the bad debt and the GST adjustment."""
    if not _require_auth(request):
        return _login()
    form = await request.form()
    body: dict[str, Any] = {}
    if _form_str(form, "write_off_date"):
        body["write_off_date"] = _form_str(form, "write_off_date")
    if _form_str(form, "reason"):
        body["reason"] = _form_str(form, "reason")
    else:
        return _back(f"/invoices/{invoice_id}/fix", request,
                     "Please say why it is being written off.")
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/invoices/{invoice_id}/write-off", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.is_success:
        return _back(f"/invoices/{invoice_id}", request,
                     "Written off as a bad debt. The GST on it has been adjusted.")
    return _back(f"/invoices/{invoice_id}/fix", request, _error_text(resp))


# ---------------------------------------------------------------------------
# History panel
# ---------------------------------------------------------------------------


@router.get("/history/{entity}/{doc_id}", response_class=HTMLResponse, response_model=None)
async def history_panel(request: Request, entity: str, doc_id: str) -> HTMLResponse:
    if not _require_auth(request):
        return HTMLResponse("", status_code=401)
    error: str | None = None
    events: list[dict] = []
    try:
        uuid.UUID(doc_id)
    except ValueError:
        return HTMLResponse("", status_code=404)
    if entity not in _HISTORY_ENTITIES:
        return HTMLResponse("", status_code=404)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/history/{entity}/{doc_id}")
    if resp.is_success:
        events = list(reversed(resp.json().get("events", [])))  # newest first
        tz = _local_zone(request)
        for ev in events:
            ev["when"] = _local_when(ev.get("at"), tz)
    elif resp.status_code == 403:
        error = "You do not have access to this history."
    else:
        error = "The history could not be loaded just now. Refresh to try again."
    return _TEMPLATES.TemplateResponse(
        request,
        "_partials/document_history_events.html",
        {"events": events, "error": error},
    )


# ---------------------------------------------------------------------------
# Approvals (P1-72)
# ---------------------------------------------------------------------------


async def _approval(request: Request, kind: str, doc_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    base = "bills" if kind == "bill" else "payments"
    form = await request.form()
    action = _form_str(form, "action")
    if action not in ("submit", "approve", "send_back"):
        return _back(f"/{base}/{doc_id}", request, "Unknown approval action.")
    body: dict[str, Any] = {"action": action}
    if _form_str(form, "note"):
        body["note"] = _form_str(form, "note")
    if kind == "bill" and _form_str(form, "planned_pay_date"):
        body["planned_pay_date"] = _form_str(form, "planned_pay_date")
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/{base}/{doc_id}/approval",
            json=body,
            headers={"If-Match": _form_str(form, "version")},
        )
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    back = _form_str(form, "next") or f"/{base}/{doc_id}"
    if not back.startswith("/") or back.startswith("//"):
        back = f"/{base}/{doc_id}"
    if resp.is_success:
        done = {
            "submit": "Sent for approval.",
            "approve": "Approved. It can now be posted.",
            "send_back": "Sent back with your note.",
        }[action]
        return _back(back, request, done)
    return _back(back, request, _error_text(resp))


@router.post("/bills/{bill_id}/approval", response_model=None)
async def bill_approval(request: Request, bill_id: str) -> RedirectResponse:
    return await _approval(request, "bill", bill_id)


@router.post("/payments/{payment_id}/approval", response_model=None)
async def payment_approval(request: Request, payment_id: str) -> RedirectResponse:
    return await _approval(request, "payment", payment_id)


@router.get("/approvals", response_class=HTMLResponse, response_model=None)
async def approvals_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return _login()
    items: list[dict] = []
    settings: dict[str, Any] = {}
    error: str | None = None
    async with api_client(request) as client:
        r = await client.get("/api/v1/approvals")
        if r.status_code == 401:
            request.session.clear()
            return _login()
        if r.is_success:
            items = r.json().get("items", [])
        else:
            error = _error_text(r)
        s = await client.get("/api/v1/approvals/settings")
        if s.is_success:
            settings = s.json()
    groups = {
        "waiting": [i for i in items if i.get("approval_status") == "AWAITING_APPROVAL"],
        "approved": [i for i in items if i.get("approval_status") == "APPROVED"],
        "sent_back": [i for i in items if i.get("approval_status") == "SENT_BACK"],
        "to_pay": [i for i in items if i.get("kind") == "bill" and not i.get("approval_status")
                   and i.get("planned_pay_date")],
    }
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "approvals/list.html",
        {
            "groups": groups,
            "settings": settings,
            "is_admin": (request.session.get("user_role") or "") in ("admin", "owner"),
            "error": error,
            "flash": flash,
        },
    )


@router.post("/approvals/settings", response_model=None)
async def approvals_settings(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return _login()
    form = await request.form()
    body = {
        "bills_need_approval": form.get("bills_need_approval") == "1",
        "payments_need_approval": form.get("payments_need_approval") == "1",
    }
    async with api_client(request) as client:
        resp = await client.put("/api/v1/approvals/settings", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    if resp.is_success:
        return _back("/approvals", request, "Approval settings saved.")
    if resp.status_code == 403:
        return _back("/approvals", request, "Only an admin can change these settings.")
    return _back("/approvals", request, _error_text(resp))
