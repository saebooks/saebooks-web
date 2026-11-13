"""Day one for a new customer (b06) — screens over the engine's setup APIs.

Route map
---------
GET  /setup                                    first-run checklist
GET  /setup/opening-balances                   conversion date + trial balance grid
POST /setup/opening-balances/conversion-date
POST /setup/opening-balances/preview           grid or CSV → preview (nothing saved)
POST /setup/opening-balances/save              post the opening journal
GET  /setup/opening-balances/template.csv      every account, ready to fill in
GET  /setup/opening-balances/check             cut-over check against the old system

GET  /admin/imports/csv/{kind}                 upload form (contacts, items, bills,
                                               open-invoices, open-bills)
POST /admin/imports/csv/{kind}/preview         per-row plan, nothing written
POST /admin/imports/csv/{kind}/commit          import
GET  /admin/imports/csv/{kind}/template        a CSV with the expected columns
GET  /admin/imports/xero                       switching from Xero (honest status)

POST /settings/users/invite                    invite by email with a role
POST /settings/users/{id}/role                 change role
POST /settings/users/{id}/remove               deactivate (or cancel an invitation)
POST /settings/users/{id}/resend               resend an invitation
GET  /settings/roles                           roles + one-click accountant role
POST /settings/roles/accountant

GET  /settings/documents                       logo, document wording, email
POST /settings/documents                       save wording / email settings
POST /settings/documents/logo                  upload logo
POST /settings/documents/logo/remove
GET  /settings/documents/logo                  the logo image (for the preview)
POST /settings/documents/test-email            send a test email to yourself
GET  /settings/numbering
POST /settings/numbering/{kind}

GET  /accept-invite?token=…                    choose a password, join the books
POST /accept-invite

The web tier holds no accounting rules: every check (balances, duplicates,
numbering collisions, placeholder names) is the engine's; this module renders
what comes back in plain words.
"""
from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.day_one")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_LONG = 120.0  # imports and the opening journal can take a while

DOC_LABELS = {
    "quote": "Quotes",
    "invoice": "Invoices",
    "credit_note": "Credit notes",
    "statement": "Statements",
    "purchase_order": "Purchase orders",
    "remittance": "Remittance advice",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _authed(request: Request) -> bool:
    return bool(request.session.get("api_token"))


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_sae_staff")) or request.session.get(
        "user_role", ""
    ) in ("owner", "admin")


def _login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _forbidden() -> HTMLResponse:
    return HTMLResponse(
        "Only an owner or administrator can do this.", status_code=403
    )


def _detail(resp: httpx.Response, fallback: str = "Something went wrong.") -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"{fallback} (HTTP {resp.status_code})"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        return "; ".join(
            str(d.get("msg", d)).removeprefix("Value error, ") for d in detail
        )
    return str(detail or fallback)


def _flash(request: Request, message: str, kind: str = "pos") -> None:
    request.session["flash"] = message
    request.session["flash_kind"] = kind


def _pop_flash(request: Request) -> tuple[str | None, str]:
    return request.session.pop("flash", None), request.session.pop("flash_kind", "pos")


def _mode(request: Request) -> str:
    return getattr(request.state, "active_company_bookkeeping_mode", "full") or "full"


def _render(request: Request, template: str, ctx: dict[str, Any], status: int = 200) -> HTMLResponse:
    flash, kind = _pop_flash(request)
    base = {"flash": flash, "flash_kind": kind, "mode": _mode(request)}
    base.update(ctx)
    return _TEMPLATES.TemplateResponse(request, template, base, status_code=status)


async def _read_upload(form: Any, field: str = "file") -> str:
    up = form.get(field)
    if up is None or not hasattr(up, "read"):
        return ""
    data = await up.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------

_CHECKLIST_LINKS = {
    "company_details": "/settings/company",
    "logo": "/settings/documents#logo",
    "bank_accounts": "/bank-accounts",
    "conversion_date": "/setup/opening-balances",
    "opening_balances": "/setup/opening-balances",
    "contacts": "/admin/imports/csv/contacts",
    "cutover_check": "/setup/opening-balances/check",
    "numbering": "/settings/numbering",
    "email": "/settings/documents#email",
    "users": "/settings/users",
}


@router.get("/setup", response_class=HTMLResponse, response_model=None)
async def setup_checklist(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.get("/api/v1/setup/checklist")
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    data = resp.json() if resp.is_success else {"items": [], "done": 0, "total": 0}
    for item in data.get("items", []):
        item["href"] = _CHECKLIST_LINKS.get(item["key"], "/settings")
    return _render(
        request,
        "day_one/checklist.html",
        {"data": data, "error": None if resp.is_success else _detail(resp)},
    )


# ---------------------------------------------------------------------------
# Opening balances
# ---------------------------------------------------------------------------


async def _postable_accounts(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = await client.get("/api/v1/accounts", params={"limit": 1000, "offset": offset})
        if not resp.is_success:
            break
        body = resp.json()
        items = body.get("items", [])
        out.extend(a for a in items if not a.get("is_header"))
        offset += len(items)
        if not items or offset >= body.get("total", 0):
            break
    out.sort(key=lambda a: a.get("code") or "")
    return out


_TYPE_ORDER = [
    ("ASSET", "Assets"),
    ("LIABILITY", "Liabilities"),
    ("EQUITY", "Equity"),
    ("INCOME", "Income"),
    ("OTHER_INCOME", "Other income"),
    ("COST_OF_SALES", "Cost of sales"),
    ("EXPENSE", "Expenses"),
    ("OTHER_EXPENSE", "Other expenses"),
]


@router.get("/setup/opening-balances", response_class=HTMLResponse, response_model=None)
async def opening_balances_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        status_resp = await client.get("/api/v1/opening-balances")
        accounts = await _postable_accounts(client)
    if status_resp.status_code == 401:
        request.session.clear()
        return _login()
    status = status_resp.json() if status_resp.is_success else {}
    saved = {ln["account_id"]: ln for ln in status.get("lines", [])}
    groups = []
    for t, label in _TYPE_ORDER:
        rows = [a for a in accounts if a.get("account_type") == t]
        if rows:
            groups.append({"label": label, "rows": rows})
    return _render(
        request,
        "day_one/opening_balances.html",
        {
            "status": status,
            "groups": groups,
            "saved": saved,
            "can_edit": _is_admin(request),
            "error": None if status_resp.is_success else _detail(status_resp),
        },
    )


@router.post("/setup/opening-balances/conversion-date", response_model=None)
async def opening_conversion_date(request: Request, conversion_date: str = Form(...)) -> RedirectResponse:
    if not _authed(request):
        return _login()
    if not _is_admin(request):
        return _forbidden()  # type: ignore[return-value]
    async with api_client(request) as client:
        resp = await client.put(
            "/api/v1/opening-balances/conversion-date",
            json={"conversion_date": conversion_date},
            timeout=_LONG,
        )
    if resp.is_success:
        _flash(request, "Conversion date saved.")
    else:
        _flash(request, _detail(resp, "Could not save the conversion date."), "neg")
    return RedirectResponse(url="/setup/opening-balances", status_code=303)


def _grid_lines(form: Any) -> list[dict[str, str]]:
    lines: dict[str, dict[str, str]] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str) or not value.strip():
            continue
        if key.startswith("dr_") or key.startswith("cr_"):
            aid = key[3:]
            ln = lines.setdefault(aid, {"account_id": aid, "debit": "0", "credit": "0"})
            ln["debit" if key.startswith("dr_") else "credit"] = (
                value.strip().replace(",", "").replace("$", "")
            )
    return list(lines.values())


@router.post("/setup/opening-balances/preview", response_class=HTMLResponse, response_model=None)
async def opening_preview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    csv_text = await _read_upload(form)
    body: dict[str, Any] = {}
    if form.get("conversion_date"):
        body["conversion_date"] = str(form.get("conversion_date"))
    if csv_text.strip():
        body["csv"] = csv_text
    else:
        body["lines"] = _grid_lines(form)
    async with api_client(request) as client:
        resp = await client.post("/api/v1/opening-balances/preview", json=body, timeout=_LONG)
    if not resp.is_success:
        _flash(request, _detail(resp, "Could not read those figures."), "neg")
        return RedirectResponse(url="/setup/opening-balances", status_code=303)
    preview = resp.json()
    save_lines = [
        {"account_id": ln["account_id"], "debit": ln["debit"], "credit": ln["credit"]}
        for ln in preview.get("lines", [])
    ]
    import json

    return _render(
        request,
        "day_one/opening_preview.html",
        {
            "p": preview,
            "lines_json": json.dumps(save_lines),
            "can_edit": _is_admin(request),
        },
    )


@router.post("/setup/opening-balances/save", response_model=None)
async def opening_save(
    request: Request,
    lines_json: str = Form(...),
    conversion_date: str = Form(""),
) -> RedirectResponse:
    import json

    if not _authed(request):
        return _login()
    if not _is_admin(request):
        return _forbidden()  # type: ignore[return-value]
    try:
        lines = json.loads(lines_json)
    except ValueError:
        _flash(request, "The figures were lost on the way. Please preview again.", "neg")
        return RedirectResponse(url="/setup/opening-balances", status_code=303)
    body: dict[str, Any] = {"lines": lines}
    if conversion_date:
        body["conversion_date"] = conversion_date
    async with api_client(request) as client:
        resp = await client.post("/api/v1/opening-balances", json=body, timeout=_LONG)
    if not resp.is_success:
        _flash(request, _detail(resp, "Could not save the opening balances."), "neg")
        return RedirectResponse(url="/setup/opening-balances", status_code=303)
    ref = resp.json().get("journal_ref")
    _flash(
        request,
        "Opening balances saved"
        + (f" (journal {ref})." if ref else ".")
        + " Now bring across unpaid invoices and bills, then check everything matches.",
    )
    return RedirectResponse(url="/setup/opening-balances/check", status_code=303)


@router.get("/setup/opening-balances/template.csv", response_model=None)
async def opening_template(request: Request) -> Response:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.get("/api/v1/opening-balances/template.csv")
    return Response(
        resp.content,
        status_code=resp.status_code,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="opening-balances.csv"'},
    )


@router.get("/setup/opening-balances/check", response_class=HTMLResponse, response_model=None)
async def opening_check(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.get("/api/v1/opening-balances/check", timeout=_LONG)
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    return _render(
        request,
        "day_one/opening_check.html",
        {
            "c": resp.json() if resp.is_success else None,
            "error": None if resp.is_success else _detail(resp),
        },
    )


# ---------------------------------------------------------------------------
# CSV imports (contacts, items, bills, unpaid invoices and bills)
# ---------------------------------------------------------------------------

IMPORT_KINDS: dict[str, dict[str, Any]] = {
    "contacts": {
        "title": "Customers and suppliers",
        "intro": (
            "Upload your contact list exported from Xero, MYOB or QuickBooks, or use our "
            "template. Contacts already in your books are matched by ABN, then by name, "
            "and only empty details are filled in — nothing is duplicated or overwritten."
        ),
        "how": [
            "Xero: Contacts → All contacts → Export → CSV.",
            "MYOB: File → Import/Export → Export Data → Cards.",
            "QuickBooks: Sales → Customers → Export to Excel (save as CSV); same for Vendors.",
        ],
        "engine": "contacts",
    },
    "items": {
        "title": "Products and services",
        "intro": (
            "Upload your item list. For stocked items, include the quantity on hand and unit "
            "cost at your conversion date — that opening stock is posted to your inventory "
            "account against opening balance equity."
        ),
        "how": [
            "Xero: Business → Products and services → Export.",
            "MYOB: File → Import/Export → Export Data → Items.",
            "QuickBooks: Sales → Products and services → Export.",
        ],
        "engine": "items",
    },
    "bills": {
        "title": "Supplier bills",
        "intro": (
            "Upload bills as drafts to review and post one by one, or — for bills still "
            "unpaid at your conversion date — choose “Unpaid at conversion” to post them "
            "straight to payables with no expense and no GST."
        ),
        "how": [
            "Columns: supplier, date, due date, reference, description, account, amount, tax code.",
        ],
        "engine": "bills",
    },
    "open-invoices": {
        "title": "Unpaid invoices at conversion",
        "intro": (
            "Every customer invoice still unpaid on your conversion date. Each comes across "
            "with its old number and due date and the amount still owing, so ageing, "
            "statements and payments work from day one. They post to receivables against "
            "opening balance equity — no income and no GST, because your old system already "
            "counted those."
        ),
        "how": [
            "Xero: Business → Invoices → Awaiting payment → Export.",
            "MYOB: Reports → Sales → Receivables Reconciliation (detail) → export as CSV.",
            "QuickBooks: Reports → Open invoices → Export to CSV.",
        ],
        "doc_kind": "invoice",
    },
    "open-bills": {
        "title": "Unpaid bills at conversion",
        "intro": (
            "Every supplier bill still unpaid on your conversion date, with the amount still "
            "owing. They post to payables against opening balance equity — no expense and no GST."
        ),
        "how": [
            "Xero: Business → Bills to pay → Awaiting payment → Export.",
            "MYOB: Reports → Purchases → Payables Reconciliation (detail) → export as CSV.",
            "QuickBooks: Reports → Unpaid bills → Export to CSV.",
        ],
        "doc_kind": "bill",
    },
}

_OPEN_DOC_TEMPLATE = (
    "Contact name,Number,Date,Due date,Amount due\r\n"
    "Example Customer Pty Ltd,INV-0412,15/06/2026,15/07/2026,1100.00\r\n"
)


def _options_from_form(kind: str, form: Any) -> dict[str, Any]:
    if kind == "contacts":
        return {"default_type": str(form.get("default_type") or "CUSTOMER")}
    if kind == "items":
        return {
            k: str(form.get(k) or "")
            for k in ("income_account", "inventory_account", "cogs_account")
        }
    if kind == "bills":
        return {
            "mode": "opening" if form.get("mode") == "opening" else "draft",
            "create_missing_suppliers": bool(form.get("create_missing")),
        }
    return {"create_missing_contacts": bool(form.get("create_missing"))}


async def _run_import(
    request: Request, kind: str, csv_text: str, options: dict[str, Any], *, commit: bool
) -> httpx.Response:
    spec = IMPORT_KINDS[kind]
    async with api_client(request) as client:
        if "doc_kind" in spec:
            url = "/api/v1/opening-balances/documents" + ("" if commit else "/preview")
            return await client.post(
                url,
                json={
                    "kind": spec["doc_kind"],
                    "csv": csv_text,
                    "create_missing_contacts": bool(options.get("create_missing_contacts")),
                },
                timeout=_LONG,
            )
        url = f"/api/v1/imports/csv/{spec['engine']}/" + ("commit" if commit else "preview")
        return await client.post(url, json={"csv": csv_text, "options": options}, timeout=_LONG)


async def _accounts_for_items(request: Request) -> list[dict[str, Any]]:
    async with api_client(request) as client:
        return await _postable_accounts(client)


@router.get("/admin/imports/csv/{kind}", response_class=HTMLResponse, response_model=None)
async def csv_import_form(request: Request, kind: str) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    if kind not in IMPORT_KINDS:
        return HTMLResponse("Unknown import", status_code=404)
    if not _is_admin(request):
        return _forbidden()
    accounts = await _accounts_for_items(request) if kind == "items" else []
    status = None
    if "doc_kind" in IMPORT_KINDS[kind] or kind == "bills":
        async with api_client(request) as client:
            r = await client.get("/api/v1/opening-balances")
        status = r.json() if r.is_success else None
    return _render(
        request,
        "day_one/import_form.html",
        {"kind": kind, "spec": IMPORT_KINDS[kind], "accounts": accounts, "ob_status": status},
    )


@router.get("/admin/imports/csv/{kind}/template", response_model=None)
async def csv_import_template(request: Request, kind: str) -> Response:
    if not _authed(request):
        return _login()
    if kind not in IMPORT_KINDS:
        return HTMLResponse("Unknown import", status_code=404)
    spec = IMPORT_KINDS[kind]
    if "doc_kind" in spec:
        body = _OPEN_DOC_TEMPLATE.encode()
    else:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/imports/csv/{spec['engine']}/template")
        body = resp.content
    return Response(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{kind}-template.csv"'},
    )


@router.post("/admin/imports/csv/{kind}/preview", response_class=HTMLResponse, response_model=None)
async def csv_import_preview(request: Request, kind: str) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    if kind not in IMPORT_KINDS:
        return HTMLResponse("Unknown import", status_code=404)
    if not _is_admin(request):
        return _forbidden()
    form = await request.form()
    csv_text = await _read_upload(form)
    if not csv_text.strip():
        _flash(request, "Choose a CSV file to upload.", "neg")
        return RedirectResponse(url=f"/admin/imports/csv/{kind}", status_code=303)
    options = _options_from_form(kind, form)
    resp = await _run_import(request, kind, csv_text, options, commit=False)
    if not resp.is_success:
        _flash(request, _detail(resp, "That file could not be read."), "neg")
        return RedirectResponse(url=f"/admin/imports/csv/{kind}", status_code=303)
    result = resp.json()
    rows = result.get("rows")
    if rows is None and "errors" in result:  # draft bills report errors only
        rows = [
            {"row": e.get("row"), "action": "error", "errors": [e.get("error")]}
            for e in result.get("errors", [])
        ]
    return _render(
        request,
        "day_one/import_preview.html",
        {
            "kind": kind,
            "spec": IMPORT_KINDS[kind],
            "result": result,
            "rows": rows or [],
            "options": options,
            "csv_b64": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
        },
    )


@router.post("/admin/imports/csv/{kind}/commit", response_class=HTMLResponse, response_model=None)
async def csv_import_commit(request: Request, kind: str) -> HTMLResponse | RedirectResponse:
    import json

    if not _authed(request):
        return _login()
    if kind not in IMPORT_KINDS:
        return HTMLResponse("Unknown import", status_code=404)
    if not _is_admin(request):
        return _forbidden()
    form = await request.form()
    try:
        csv_text = base64.b64decode(str(form.get("csv_b64") or ""), validate=True).decode("utf-8")
        options = json.loads(str(form.get("options_json") or "{}"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        _flash(request, "The file was lost on the way. Please upload it again.", "neg")
        return RedirectResponse(url=f"/admin/imports/csv/{kind}", status_code=303)
    resp = await _run_import(request, kind, csv_text, options, commit=True)
    if not resp.is_success:
        _flash(request, _detail(resp, "The import did not go through."), "neg")
        return RedirectResponse(url=f"/admin/imports/csv/{kind}", status_code=303)
    result = resp.json()
    created = result.get("created", 0)
    updated = result.get("updated", 0)
    msg = f"Imported: {created} added"
    if updated:
        msg += f", {updated} updated"
    skipped = (result.get("skipped") or 0) + (
        result.get("errors") if isinstance(result.get("errors"), int) else 0
    )
    if skipped:
        msg += f", {skipped} left out (see the preview for why)"
    _flash(request, msg + ".")
    next_url = {
        "contacts": "/contacts",
        "items": "/items",
        "bills": "/bills",
        "open-invoices": "/setup/opening-balances/check",
        "open-bills": "/setup/opening-balances/check",
    }[kind]
    return RedirectResponse(url=next_url, status_code=303)


@router.get("/admin/imports/xero", response_class=HTMLResponse, response_model=None)
async def xero_switch(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    return _render(request, "day_one/xero.html", {"kinds": IMPORT_KINDS})


# ---------------------------------------------------------------------------
# Users, invitations and roles
# ---------------------------------------------------------------------------


@router.post("/settings/users/invite", response_class=HTMLResponse, response_model=None)
async def users_invite(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    role: str = Form("bookkeeper"),
) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    role_id = None
    if role.startswith("role:"):
        role_id, role = role[5:].split("|", 1)
    payload: dict[str, Any] = {"email": email.strip(), "role": role}
    if display_name.strip():
        payload["display_name"] = display_name.strip()
    if role_id:
        payload["role_id"] = role_id
    async with api_client(request) as client:
        resp = await client.post("/api/v1/users/invite", json=payload, timeout=30.0)
    if not resp.is_success:
        _flash(request, _detail(resp, "The invitation was not sent."), "neg")
        return RedirectResponse(url="/settings/users", status_code=303)
    body = resp.json()
    if body.get("email_sent"):
        _flash(request, f"Invitation emailed to {body.get('email')}.")
    else:
        request.session["invite_link"] = {
            "email": body.get("email"),
            "url": body.get("accept_url"),
            "detail": body.get("detail"),
        }
    return RedirectResponse(url="/settings/users", status_code=303)


async def _user_version(request: Request, user_id: str) -> int | None:
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/users/{user_id}")
    if not resp.is_success:
        return None
    return resp.json().get("version")


@router.post("/settings/users/{user_id}/role", response_model=None)
async def users_change_role(request: Request, user_id: str, role: str = Form(...)) -> RedirectResponse:
    if not _authed(request):
        return _login()
    version = await _user_version(request, user_id)
    if version is None:
        _flash(request, "That person could not be found.", "neg")
        return RedirectResponse(url="/settings/users", status_code=303)
    payload: dict[str, Any] = {"role": role, "role_id": None}
    if role.startswith("role:"):
        rid, base = role[5:].split("|", 1)
        payload = {"role": base, "role_id": rid}
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/users/{user_id}", json=payload, headers={"If-Match": str(version)}
        )
    if resp.is_success:
        _flash(request, "Role changed.")
    else:
        _flash(request, _detail(resp, "The role was not changed."), "neg")
    return RedirectResponse(url="/settings/users", status_code=303)


@router.post("/settings/users/{user_id}/remove", response_model=None)
async def users_remove(request: Request, user_id: str) -> RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        target = await client.get(f"/api/v1/users/{user_id}")
    me = str(request.session.get("username") or "").lower()
    if target.is_success and me and me in {
        str(target.json().get("username") or "").lower(),
        str(target.json().get("email") or "").lower(),
    }:
        _flash(request, "You cannot remove yourself.", "neg")
        return RedirectResponse(url="/settings/users", status_code=303)
    version = target.json().get("version") if target.is_success else None
    if version is None:
        _flash(request, "That person could not be found.", "neg")
        return RedirectResponse(url="/settings/users", status_code=303)
    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/users/{user_id}", headers={"If-Match": str(version)}
        )
    if resp.status_code in (200, 204):
        _flash(request, "Access removed. They can no longer sign in.")
    else:
        _flash(request, _detail(resp, "Access was not removed."), "neg")
    return RedirectResponse(url="/settings/users", status_code=303)


@router.post("/settings/users/{user_id}/resend", response_model=None)
async def users_resend(request: Request, user_id: str) -> RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/users/{user_id}/resend-invite", timeout=30.0)
    if not resp.is_success:
        _flash(request, _detail(resp, "The invitation was not resent."), "neg")
    else:
        body = resp.json()
        if body.get("email_sent"):
            _flash(request, f"Invitation sent again to {body.get('email')}.")
        else:
            request.session["invite_link"] = {
                "email": body.get("email"),
                "url": body.get("accept_url"),
                "detail": body.get("detail"),
            }
    return RedirectResponse(url="/settings/users", status_code=303)


@router.get("/settings/roles", response_class=HTMLResponse, response_model=None)
async def roles_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.get("/api/v1/roles")
        perms = await client.get("/api/v1/permissions")
    degraded = resp.status_code == 404
    roles = resp.json() if resp.is_success else []
    details = []
    if resp.is_success:
        async with api_client(request) as client:
            for r in roles:
                d = await client.get(f"/api/v1/roles/{r['id']}")
                if d.is_success:
                    details.append(d.json())
    return _render(
        request,
        "day_one/roles.html",
        {
            "roles": details,
            "degraded": degraded,
            "permission_count": len(perms.json()) if perms.is_success else None,
            "error": None if (resp.is_success or degraded) else _detail(resp),
            "has_accountant": any(r.get("name", "").lower() == "accountant" for r in roles),
        },
    )


@router.post("/settings/roles/accountant", response_model=None)
async def roles_add_accountant(request: Request) -> RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/roles/from-template", json={"template": "accountant", "name": "Accountant"}
        )
    if resp.is_success:
        _flash(request, "Accountant role added. Choose it when you invite your accountant.")
    else:
        _flash(request, _detail(resp, "The role was not added."), "neg")
    return RedirectResponse(url="/settings/roles", status_code=303)


# ---------------------------------------------------------------------------
# Accept an invitation (public — the invitee has no session yet)
# ---------------------------------------------------------------------------


@router.get("/accept-invite", response_class=HTMLResponse, response_model=None)
async def accept_invite_page(request: Request, token: str | None = None) -> HTMLResponse:
    if not token:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_email_error.html",
            {"message": "This invitation link is incomplete. Ask for a new one."},
            status_code=400,
        )
    return _TEMPLATES.TemplateResponse(
        request, "day_one/accept_invite.html", {"token": token, "error": None}
    )


@router.post("/accept-invite", response_model=None)
async def accept_invite_submit(
    request: Request, token: str = Form(...), password: str = Form(...)
) -> Any:
    from saebooks_web.routes.public_auth import _api_client, _login_with_jwt

    try:
        async with _api_client() as client:
            resp = await client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"token": token, "new_password": password},
            )
    except httpx.RequestError:
        return _TEMPLATES.TemplateResponse(
            request,
            "day_one/accept_invite.html",
            {"token": token, "error": "We could not reach the server. Please try again."},
            status_code=502,
        )
    if resp.is_success:
        access = resp.json().get("access_token")
        if access:
            await _login_with_jwt(request, access)
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code in (404, 410):
        msg = "This invitation has expired or has already been used. Ask for a new one."
    else:
        msg = _detail(resp, "That password was not accepted.")
    return _TEMPLATES.TemplateResponse(
        request,
        "day_one/accept_invite.html",
        {"token": token, "error": msg},
        status_code=resp.status_code if resp.status_code < 500 else 502,
    )


# ---------------------------------------------------------------------------
# Documents & email settings, logo
# ---------------------------------------------------------------------------


@router.get("/settings/documents", response_class=HTMLResponse, response_model=None)
async def documents_page(request: Request) -> HTMLResponse | RedirectResponse:
    from saebooks_web.comms import customer_email_status

    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        ds = await client.get("/api/v1/setup/document-settings")
        logo = await client.get("/api/v1/setup/logo")
    if ds.status_code == 401:
        request.session.clear()
        return _login()
    settings_ = ds.json() if ds.is_success else None
    status = customer_email_status(
        tenant_id=(settings_ or {}).get("tenant_id"),
        from_addr=None,
        tenant_outbound_enabled=bool(settings_ and settings_.get("outbound_email_enabled")),
        company_addresses=(settings_ or {}).get("from_addresses") or [],
    )
    return _render(
        request,
        "day_one/documents.html",
        {
            "s": settings_,
            "logo": logo.json() if logo.is_success else None,
            "doc_labels": DOC_LABELS,
            "email_status": status,
            "can_edit": _is_admin(request),
            "error": None if ds.is_success else _detail(ds),
        },
    )


@router.post("/settings/documents", response_model=None)
async def documents_save(request: Request) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    section = str(form.get("section") or "")
    patch: dict[str, Any] = {}
    if section == "email":
        patch = {
            "sender_name": str(form.get("sender_name") or "").strip() or None,
            "reply_to": str(form.get("reply_to") or "").strip() or None,
            "signature": str(form.get("signature") or "").strip() or None,
            "bcc_self": bool(form.get("bcc_self")),
            "from_addresses": [
                a.strip()
                for a in str(form.get("from_addresses") or "").replace(";", ",").replace("\n", ",").split(",")
                if a.strip()
            ],
        }
    elif section == "templates":
        templates: dict[str, Any] = {}
        for doc in DOC_LABELS:
            subj = str(form.get(f"subject_{doc}") or "").strip()
            body = str(form.get(f"body_{doc}") or "").replace("\r\n", "\n").strip()
            if form.get(f"reset_{doc}"):
                templates[doc] = None
            else:
                templates[doc] = {"subject": subj, "body": body}
        patch = {"templates": templates}
    elif section == "documents":
        patch = {
            "document_footer": str(form.get("document_footer") or "").strip() or None,
            "accent_colour": str(form.get("accent_colour") or "").strip() or None,
            "quote_title": str(form.get("quote_title") or "").strip() or None,
            "quote_terms": str(form.get("quote_terms") or "").replace("\r\n", "\n").strip() or None,
            "quote_acceptance": str(form.get("quote_acceptance") or "").strip() or None,
        }
    else:
        _flash(request, "Nothing to save.", "neg")
        return RedirectResponse(url="/settings/documents", status_code=303)
    async with api_client(request) as client:
        resp = await client.patch("/api/v1/setup/document-settings", json=patch)
    if resp.is_success:
        _flash(request, "Saved.")
    else:
        _flash(request, _detail(resp, "Not saved."), "neg")
    return RedirectResponse(url=f"/settings/documents#{section}", status_code=303)


@router.post("/settings/documents/logo", response_model=None)
async def documents_logo_upload(request: Request) -> RedirectResponse:
    if not _authed(request):
        return _login()
    form = await request.form()
    up = form.get("logo")
    data = await up.read() if up is not None and hasattr(up, "read") else b""
    if not data:
        _flash(request, "Choose an image file.", "neg")
        return RedirectResponse(url="/settings/documents#logo", status_code=303)
    if len(data) > 1_000_000:
        _flash(request, "The logo must be smaller than 1 MB.", "neg")
        return RedirectResponse(url="/settings/documents#logo", status_code=303)
    async with api_client(request) as client:
        resp = await client.put(
            "/api/v1/setup/logo", json={"data_b64": base64.b64encode(data).decode("ascii")}
        )
    if resp.is_success:
        _flash(request, "Logo saved. It now prints on your quotes, invoices and other documents.")
    else:
        _flash(request, _detail(resp, "The logo was not saved."), "neg")
    return RedirectResponse(url="/settings/documents#logo", status_code=303)


@router.post("/settings/documents/logo/remove", response_model=None)
async def documents_logo_remove(request: Request) -> RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.delete("/api/v1/setup/logo")
    if resp.status_code in (200, 204):
        _flash(request, "Logo removed.")
    else:
        _flash(request, _detail(resp, "The logo was not removed."), "neg")
    return RedirectResponse(url="/settings/documents#logo", status_code=303)


@router.get("/settings/documents/logo", response_model=None)
async def documents_logo_image(request: Request) -> Response:
    if not _authed(request):
        return Response(status_code=401)
    async with api_client(request) as client:
        resp = await client.get("/api/v1/setup/logo/image")
    if not resp.is_success:
        return Response(status_code=404)
    return Response(
        resp.content,
        media_type=resp.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.post("/settings/documents/test-email", response_model=None)
async def documents_test_email(request: Request, to: str = Form(...)) -> RedirectResponse:
    """Send a short test through the real customer-email policy and
    transport, so the result is exactly what a quote or invoice would get."""
    from saebooks_web.comms import run_customer_email_policy

    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        ds = await client.get("/api/v1/setup/document-settings")
    s = ds.json() if ds.is_success else {}
    from_addr = settings.email_platform_from or ((s.get("from_addresses") or [None])[0]) or ""
    outcome, _pid, detail = await run_customer_email_policy(
        tenant_id=s.get("tenant_id"),
        from_addr=from_addr,
        to=[to.strip()],
        cc=[],
        bcc=[],
        subject=f"Test email from {s.get('sender_name') or 'your books'}",
        body_html=(
            "<p>This is a test from your accounting software. If you can read it, "
            "emails to your customers will arrive the same way.</p>"
        ),
        body_text=None,
        attachments=[],
        tenant_outbound_enabled=bool(s.get("outbound_email_enabled")),
        reply_to=s.get("reply_to"),
        from_name=s.get("sender_name"),
    )
    if outcome == "sent":
        _flash(request, f"Test email sent to {to}. Check that it arrived.")
    elif outcome == "drafted":
        _flash(request, "The test was saved as a draft for review, not sent (draft mode is on).", "warm")
    else:
        _flash(request, f"The test email was not sent: {detail}", "neg")
    return RedirectResponse(url="/settings/documents#email", status_code=303)


@router.get("/settings/numbering", response_class=HTMLResponse, response_model=None)
async def numbering_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.get("/api/v1/setup/numbering")
    if resp.status_code == 401:
        request.session.clear()
        return _login()
    return _render(
        request,
        "day_one/numbering.html",
        {
            "counters": resp.json().get("counters", []) if resp.is_success else [],
            "can_edit": _is_admin(request),
            "error": None if resp.is_success else _detail(resp),
        },
    )


@router.post("/settings/numbering/{kind}", response_model=None)
async def numbering_save(
    request: Request,
    kind: str,
    prefix: str = Form(""),
    next_value: int = Form(...),
    pad_width: int = Form(...),
) -> RedirectResponse:
    if not _authed(request):
        return _login()
    async with api_client(request) as client:
        resp = await client.put(
            f"/api/v1/setup/numbering/{kind}",
            json={"prefix": prefix.strip(), "next_value": next_value, "pad_width": pad_width},
        )
    if resp.is_success:
        _flash(request, f"Saved. The next one will be {resp.json().get('next_number')}.")
    else:
        _flash(request, _detail(resp, "Not saved."), "neg")
    return RedirectResponse(url="/settings/numbering", status_code=303)
