"""Loans and finance screens — P0-44, batch 14.

GET  /loans                                  — list with what is owed
GET  /loans/new                              — wizard: kind, lender, what it paid for, terms, accounts
GET  /loans/preview                          — HTMX partial: repayment and schedule (posts nothing)
POST /loans/new                              — record it (posts the opening entry)
GET  /loans/repay?statement_line_id=…        — record a repayment straight from a bank line
POST /loans/repay                            — post it and match the line
GET  /loans/{id}                             — position, next repayment, schedule, repayments
POST /loans/{id}/repayments                  — record a repayment
POST /loans/{id}/repayments/{rid}/reverse    — reverse one repayment
POST /loans/{id}/cancel                      — undo a loan entered by mistake

Engine: ``/api/v1/loans`` (``saebooks/services/loans.py``). Every entry is
posted by the engine — the opening entry (purchase or money received,
liability, and for hire purchase the unexpired interest) and each repayment
split into principal and interest.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

KINDS = [
    {"value": "loan", "label": "Business loan",
     "help": "Money borrowed and paid into your bank account, or used to buy something."},
    {"value": "chattel_mortgage", "label": "Chattel mortgage",
     "help": "The usual finance for a vehicle or equipment. You own it from day one; GST on the price is claimed up front."},
    {"value": "hire_purchase", "label": "Hire purchase",
     "help": "You pay for the asset over time. The total interest is recorded up front as interest not yet charged and moved to expense with each repayment."},
]
FREQUENCIES = [
    {"value": "monthly", "label": "Monthly"},
    {"value": "fortnightly", "label": "Fortnightly"},
    {"value": "weekly", "label": "Weekly"},
    {"value": "quarterly", "label": "Quarterly"},
]


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return f"Something went wrong (HTTP {resp.status_code})."
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        loc = [str(p) for p in detail[0].get("loc", []) if p != "body"]
        msg = str(detail[0].get("msg", ""))
        return f"{' '.join(loc)}: {msg}" if loc else msg
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return f"Something went wrong (HTTP {resp.status_code})."


async def _accounts(client, *types: str) -> list[dict]:
    out: list[dict] = []
    for t in types:
        resp = await client.get(
            "/api/v1/accounts", params={"account_type": t, "limit": 500, "offset": 0}
        )
        if resp.is_success:
            out.extend(a for a in resp.json().get("items", []) if not a.get("is_header"))
    out.sort(key=lambda a: a.get("code") or "")
    return out


def _pick(accounts: list[dict], code: str) -> str:
    for a in accounts:
        if a.get("code") == code:
            return str(a["id"])
    return ""


async def _form_lists(request: Request) -> dict:
    async with api_client(request) as client:
        balance_sheet = await _accounts(client, "ASSET", "LIABILITY", "EQUITY")
        expense = await _accounts(client, "EXPENSE", "OTHER_EXPENSE")
        contacts: list[dict] = []
        resp = await client.get("/api/v1/contacts", params={"limit": 500, "offset": 0})
        if resp.is_success:
            contacts = resp.json().get("items", [])
        tax_codes: list[dict] = []
        resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if resp.is_success:
            body = resp.json()
            tax_codes = body.get("items", []) if isinstance(body, dict) else body
        dep_models: list[dict] = []
        resp = await client.get("/api/v1/depreciation_models", params={"limit": 100})
        if resp.is_success:
            dep_models = resp.json().get("items", [])
    return {
        "assets": [a for a in balance_sheet if a.get("account_type") == "ASSET"],
        "liabilities": [a for a in balance_sheet if a.get("account_type") == "LIABILITY"],
        "balance_sheet": balance_sheet,
        "expense_accounts": expense,
        "contacts": contacts,
        "tax_codes": tax_codes,
        "dep_models": dep_models,
        "kinds": KINDS,
        "frequencies": FREQUENCIES,
    }


def _defaults(lists: dict) -> dict[str, str]:
    today = date.today()
    first = date(today.year + (today.month // 12), today.month % 12 + 1, 1)
    return {
        "kind": "loan",
        "funding": "cash",
        "frequency": "monthly",
        "start_date": today.isoformat(),
        "first_repayment_date": first.isoformat(),
        "liability_account_id": _pick(lists["liabilities"], "2-2500"),
        "interest_account_id": _pick(lists["expense_accounts"], "6-1930"),
        "unexpired_interest_account_id": _pick(lists["liabilities"], "2-2510"),
        "drawdown_account_id": _pick(lists["assets"], "1-1110"),
        "deposit_account_id": _pick(lists["assets"], "1-1110"),
        "cost_account_id": _pick(lists["assets"], "1-3410"),
        "accum_dep_account_id": _pick(lists["assets"], "1-3420"),
        "dep_expense_account_id": _pick(lists["expense_accounts"], "6-1500"),
    }


def _payload_from_form(form: dict[str, str]) -> dict:
    def g(k: str) -> str:
        return (form.get(k) or "").strip()

    payload: dict[str, object] = {
        "name": g("name"),
        "kind": g("kind") or "loan",
        "start_date": g("start_date"),
        "first_repayment_date": g("first_repayment_date"),
        "annual_rate_pct": g("annual_rate_pct") or "0",
        "term_periods": g("term_periods") or "0",
        "frequency": g("frequency") or "monthly",
        "liability_account_id": g("liability_account_id") or None,
        "interest_account_id": g("interest_account_id") or None,
        "funding": g("funding") or "cash",
    }
    for opt in ("lender_contact_id", "reference", "notes", "repayment_amount"):
        if g(opt):
            payload[opt] = g(opt)
    if g("balloon"):
        payload["balloon"] = g("balloon")
    if payload["kind"] == "hire_purchase" and g("unexpired_interest_account_id"):
        payload["unexpired_interest_account_id"] = g("unexpired_interest_account_id")
    if payload["funding"] == "asset":
        asset: dict[str, object] = {
            "name": g("asset_name") or g("name"),
            "depreciation_model_id": g("depreciation_model_id"),
            "cost_account_id": g("cost_account_id") or None,
            "accum_dep_account_id": g("accum_dep_account_id") or None,
            "dep_expense_account_id": g("dep_expense_account_id") or None,
            "cost": g("asset_cost") or "0",
            "gst_amount": g("asset_gst_amount") or "0",
            "deposit": g("deposit") or "0",
        }
        if g("asset_tax_code_id"):
            asset["tax_code_id"] = g("asset_tax_code_id")
        if g("deposit_account_id") and g("deposit") not in ("", "0", "0.00"):
            asset["deposit_account_id"] = g("deposit_account_id")
        if g("serial_number"):
            asset["serial_number"] = g("serial_number")
        payload["asset"] = asset
    else:
        payload["principal"] = g("principal") or None
        payload["drawdown_account_id"] = g("drawdown_account_id") or None
    return payload


@router.get("/loans", response_class=HTMLResponse, response_model=None)
async def loans_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get("/api/v1/loans")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "loans/list.html",
        {
            "loans": resp.json().get("items", []) if resp.is_success else [],
            "error": None if resp.is_success else _detail(resp),
            "flash": request.session.pop("flash", None),
            "kinds": {k["value"]: k["label"] for k in KINDS},
        },
    )


@router.get("/loans/new", response_class=HTMLResponse, response_model=None)
async def loan_new_form(request: Request, funding: str | None = None) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    lists = await _form_lists(request)
    form = _defaults(lists)
    if funding in ("cash", "asset"):
        form["funding"] = funding
        if funding == "asset":
            form["kind"] = "chattel_mortgage"
    return _TEMPLATES.TemplateResponse(
        request,
        "loans/new.html",
        {"form": form, "errors": {}, "idempotency_key": str(uuid.uuid4()), **lists},
    )


@router.get("/loans/preview", response_class=HTMLResponse, response_model=None)
async def loan_preview(request: Request) -> HTMLResponse | RedirectResponse:
    """HTMX partial — what the repayments will be. Nothing is recorded."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    q = dict(request.query_params)
    body: dict[str, object] = {
        "annual_rate_pct": q.get("annual_rate_pct") or "0",
        "term_periods": q.get("term_periods") or "0",
        "frequency": q.get("frequency") or "monthly",
        "first_repayment_date": q.get("first_repayment_date") or date.today().isoformat(),
        "balloon": q.get("balloon") or "0",
    }
    if q.get("repayment_amount"):
        body["repayment_amount"] = q["repayment_amount"]
    if q.get("funding") == "asset":
        body["asset_cost"] = q.get("asset_cost") or "0"
        body["asset_gst"] = q.get("asset_gst_amount") or "0"
        body["deposit"] = q.get("deposit") or "0"
    else:
        body["principal"] = q.get("principal") or None
    try:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/loans/preview", json=body)
        preview = resp.json() if resp.is_success else None
        error = None if resp.is_success else _detail(resp)
    except Exception:
        preview, error = None, "The preview is not available right now."
    return _TEMPLATES.TemplateResponse(
        request, "loans/_preview.html", {"preview": preview, "error": error}
    )


@router.post("/loans/new", response_class=HTMLResponse, response_model=None)
async def loan_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}
    key = form.get("idempotency_key") or str(uuid.uuid4())
    payload = _payload_from_form(form)
    async with api_client(request) as client:
        for _attempt in range(4):
            resp = await client.post(
                "/api/v1/loans", json=payload, headers={"X-Idempotency-Key": key}
            )
            if resp.status_code != 503:
                break
            await asyncio.sleep(0.5)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        loan = resp.json()
        msg = "Loan recorded. The opening entry is posted."
        if loan.get("fixed_asset_id"):
            msg += " The asset has been added to the asset register."
        request.session["flash"] = msg
        return RedirectResponse(url=f"/loans/{loan['id']}", status_code=303)
    lists = await _form_lists(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "loans/new.html",
        {"form": form, "errors": {"__all__": _detail(resp)}, "idempotency_key": key, **lists},
        status_code=422,
    )


@router.get("/loans/repay", response_class=HTMLResponse, response_model=None)
async def loan_repay_from_line(
    request: Request, statement_line_id: str | None = None
) -> HTMLResponse | RedirectResponse:
    """Record a repayment straight from a bank statement line.

    Reconciliation links here with ``?statement_line_id=``; the amount, date
    and bank account come from the line, and posting matches the line.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    line = None
    async with api_client(request) as client:
        if statement_line_id:
            resp = await client.get(f"/api/v1/bank_statement_lines/{statement_line_id}")
            if resp.is_success:
                line = resp.json()
        l_resp = await client.get("/api/v1/loans", params={"status": "active"})
    loans = l_resp.json().get("items", []) if l_resp.is_success else []
    return _TEMPLATES.TemplateResponse(
        request,
        "loans/repay.html",
        {
            "line": line,
            "statement_line_id": statement_line_id or "",
            "loans": loans,
            "flash": request.session.pop("flash", None),
            "idempotency_key": str(uuid.uuid4()),
        },
    )


async def _post_repayment(request: Request, loan_id: str, form: dict[str, str]):
    body: dict[str, object] = {}
    for k in ("amount", "repayment_date", "bank_account_id", "interest", "fees",
              "fees_account_id", "bank_statement_line_id", "notes"):
        v = (form.get(k) or "").strip()
        if v:
            body[k] = v
    key = form.get("idempotency_key") or str(uuid.uuid4())
    async with api_client(request) as client:
        for _attempt in range(4):
            resp = await client.post(
                f"/api/v1/loans/{loan_id}/repayments", json=body,
                headers={"X-Idempotency-Key": key},
            )
            if resp.status_code != 503:
                break
            await asyncio.sleep(0.5)
    return resp


@router.post("/loans/repay", response_model=None)
async def loan_repay_from_line_post(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = {k: str(v) for k, v in (await request.form()).items()}
    loan_id = (form.get("loan_id") or "").strip()
    if not loan_id:
        request.session["flash"] = "Choose which loan this repayment is for."
        return RedirectResponse(
            url=f"/loans/repay?statement_line_id={form.get('bank_statement_line_id', '')}",
            status_code=303,
        )
    resp = await _post_repayment(request, loan_id, form)
    if resp.status_code == 201:
        request.session["flash"] = "Repayment recorded and the bank line matched."
        return RedirectResponse(url=f"/loans/{loan_id}", status_code=303)
    request.session["flash"] = _detail(resp)
    return RedirectResponse(
        url=f"/loans/repay?statement_line_id={form.get('bank_statement_line_id', '')}",
        status_code=303,
    )


@router.post("/loans/{loan_id}/repayments", response_model=None)
async def loan_repayment_post(request: Request, loan_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = {k: str(v) for k, v in (await request.form()).items()}
    resp = await _post_repayment(request, loan_id, form)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        r = resp.json()
        request.session["flash"] = (
            f"Repayment of {r.get('amount')} recorded: {r.get('principal')} off the loan, "
            f"{r.get('interest')} interest."
        )
    else:
        request.session["flash"] = _detail(resp)
    return RedirectResponse(url=f"/loans/{loan_id}", status_code=303)


@router.post("/loans/{loan_id}/repayments/{repayment_id}/reverse", response_model=None)
async def loan_repayment_reverse(
    request: Request, loan_id: str, repayment_id: str
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/loans/{loan_id}/repayments/{repayment_id}/reverse", json={}
        )
    request.session["flash"] = "Repayment reversed." if resp.is_success else _detail(resp)
    return RedirectResponse(url=f"/loans/{loan_id}", status_code=303)


@router.post("/loans/{loan_id}/cancel", response_model=None)
async def loan_cancel(request: Request, loan_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    headers = {}
    if form.get("version"):
        headers["If-Match"] = str(form.get("version"))
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/loans/{loan_id}/cancel", json={}, headers=headers)
    request.session["flash"] = (
        "Loan cancelled. Its opening entry has been reversed." if resp.is_success
        else _detail(resp)
    )
    return RedirectResponse(url=f"/loans/{loan_id}", status_code=303)


@router.get("/loans/{loan_id}", response_class=HTMLResponse, response_model=None)
async def loan_detail(request: Request, loan_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/loans/{loan_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        loan = resp.json() if resp.is_success else None
        banks: list[dict] = []
        accounts_by_id: dict[str, dict] = {}
        lender = None
        if loan:
            all_accts = await _accounts(client, "ASSET", "LIABILITY", "EXPENSE", "OTHER_EXPENSE")
            accounts_by_id = {str(a["id"]): a for a in all_accts}
            banks = [a for a in all_accts if a.get("account_type") in ("ASSET", "LIABILITY")]
            if loan.get("lender_contact_id"):
                c = await client.get(f"/api/v1/contacts/{loan['lender_contact_id']}")
                if c.is_success:
                    lender = c.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "loans/detail.html",
        {
            "loan": loan,
            "error": None if loan else ("Loan not found." if resp.status_code == 404 else _detail(resp)),
            "flash": request.session.pop("flash", None),
            "banks": banks,
            "accounts_by_id": accounts_by_id,
            "lender": lender,
            "kinds": {k["value"]: k["label"] for k in KINDS},
            "today": date.today().isoformat(),
            "idempotency_key": str(uuid.uuid4()),
            "default_bank": _pick(banks, "1-1110"),
        },
        status_code=200 if loan else (404 if resp.status_code == 404 else resp.status_code),
    )
