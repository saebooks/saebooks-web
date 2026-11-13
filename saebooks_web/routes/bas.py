"""BAS screens (batch 5, GST and BAS correctness).

GET  /reports/gst-detail          — the transactions behind one BAS label, with a tie-out
GET  /bas                         — BAS periods: saved, lodged, settled; what is waiting
POST /bas/returns                 — save the BAS for a period (engine works it out)
POST /bas/returns/{id}/lodge      — record a lodgement made at the ATO (date + reference, optional lock)
GET  /bas/settle                  — preview what settling a period posts; pick the bank account
POST /bas/settle                  — clear GST and tax withheld to the ATO account, optionally pay

Every figure comes from the engine (``/api/v1/bas/*``). Nothing here lodges
with the ATO.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_LABELS = {
    "G1": "Total sales, including GST",
    "G2": "Export sales",
    "G3": "Other GST-free sales",
    "G10": "Capital purchases, including GST",
    "G11": "Other purchases, including GST",
    "1A": "GST on sales",
    "1B": "GST on purchases",
    "W1": "Total wages (finalised pay runs)",
    "W2": "Tax withheld from wages",
}

# Where each journal source type opens in this app.
_SOURCE_URLS = {
    "invoice": "/invoices/{id}",
    "bill": "/bills/{id}",
    "expense": "/expenses/{id}",
    "credit_note": "/credit-notes/{id}",
    "supplier_credit_note": "/supplier-credit-notes/{id}",
    "receipt": "/receipts/{id}",
    "payment": "/payments/{id}",
    "pay_run": "/pay-run/{id}",
}


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _detail(resp) -> str:  # type: ignore[no-untyped-def]
    try:
        body = resp.json()
        detail = body.get("detail") or body.get("message")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
        return str(detail or f"HTTP {resp.status_code}")
    except Exception:
        return f"HTTP {resp.status_code}"


def _row_url(row: dict) -> str | None:
    st = (row.get("source_type") or "").lower()
    sid = row.get("source_id")
    if st in _SOURCE_URLS and sid:
        return _SOURCE_URLS[st].format(id=sid)
    if row.get("journal_entry_id"):
        return f"/journal-entries/{row['journal_entry_id']}"
    return None


# ---------------------------------------------------------------------------
# GST detail drill-down
# ---------------------------------------------------------------------------


@router.get("/reports/gst-detail", response_class=HTMLResponse, response_model=None)
async def gst_detail(
    request: Request,
    label: str = "G1",
    from_date: str | None = None,
    to_date: str | None = None,
    basis: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    label = (label or "G1").upper()
    today = date.today()
    from_ = from_date or today.replace(day=1).isoformat()
    to_ = to_date or today.isoformat()
    detail: dict = {}
    error: str | None = None
    degraded = False
    params: dict = {"label": label, "from_date": from_, "to_date": to_}
    if basis in ("accrual", "cash"):
        params["basis"] = basis
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/bas/gst_detail", params=params)
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                detail = resp.json()
            else:
                error = _detail(resp)
    except ModuleUnavailable:
        degraded = True
    for row in detail.get("rows", []):
        row["url"] = _row_url(row)
    ctx = {
        "label": label,
        "label_text": _(_LABELS.get(label, label)),
        "labels": list(_LABELS),
        "detail": detail,
        "from_date": from_,
        "to_date": to_,
        "basis": detail.get("basis") or basis or "accrual",
        "error": error,
        "degraded": degraded,
        "back_url": "/reports/bas-summary?" + urlencode({"from_date": from_, "to_date": to_}),
    }
    return _TEMPLATES.TemplateResponse(request, "reports/gst_detail.html", ctx)


# ---------------------------------------------------------------------------
# BAS periods
# ---------------------------------------------------------------------------


@router.get("/bas", response_class=HTMLResponse, response_model=None)
async def bas_periods(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    data: dict = {}
    error: str | None = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/bas/periods")
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                data = resp.json()
            else:
                error = _detail(resp)
    except ModuleUnavailable:
        degraded = True
    flash = request.session.pop("flash", None)
    ctx = {
        "data": data,
        "items": data.get("items", []),
        "waiting": data.get("waiting", []),
        "error": error,
        "degraded": degraded,
        "flash": flash,
        "today": date.today().isoformat(),
    }
    return _TEMPLATES.TemplateResponse(request, "bas/list.html", ctx)


@router.post("/bas/returns", response_model=None)
async def bas_save(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body: dict = {
        "from_date": str(form.get("from_date", "")),
        "to_date": str(form.get("to_date", "")),
    }
    instalment = str(form.get("payg_instalment", "") or "").strip()
    if instalment:
        body["payg_instalment"] = instalment
    async with api_client(request) as client:
        resp = await client.post("/api/v1/bas/returns", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        request.session["flash"] = _("BAS saved for %(from)s to %(to)s.") % {
            "from": body["from_date"],
            "to": body["to_date"],
        }
    else:
        request.session["flash"] = _("The BAS could not be saved: %(why)s") % {"why": _detail(resp)}
    return RedirectResponse(url="/bas", status_code=303)


@router.post("/bas/returns/{return_id}/lodge", response_model=None)
async def bas_mark_lodged(request: Request, return_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body = {
        "lodged_on": str(form.get("lodged_on", "") or date.today().isoformat()),
        "reference": (str(form.get("reference", "") or "").strip() or None),
        "lock_period": str(form.get("lock_period", "")) in ("1", "on", "true"),
    }
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/bas/returns/{return_id}/lodge", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        out = resp.json()
        msg = _("BAS recorded as lodged.")
        lock = out.get("lock") or {}
        if lock.get("note"):
            msg += " " + str(lock["note"])
        elif lock.get("locked_through"):
            msg += " " + _("The books are now locked through %(date)s.") % {"date": lock["locked_through"]}
        request.session["flash"] = msg
    elif resp.status_code == 403:
        request.session["flash"] = _("Only an administrator can lock the books. Record it as lodged without locking, or ask an administrator.")
    else:
        request.session["flash"] = _("Could not record the lodgement: %(why)s") % {"why": _detail(resp)}
    return RedirectResponse(url="/bas", status_code=303)


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


@router.get("/bas/settle", response_class=HTMLResponse, response_model=None)
async def bas_settle_preview(
    request: Request, from_date: str, to_date: str
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    plan: dict = {}
    banks: list = []
    error: str | None = None
    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/bas/settle/preview", params={"from_date": from_date, "to_date": to_date}
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            plan = resp.json()
        else:
            error = _detail(resp)
        bresp = await client.get("/api/v1/bank_accounts", params={"page": 1, "page_size": 200})
        if bresp.is_success:
            payload = bresp.json()
            banks = payload.get("items", payload) if isinstance(payload, dict) else payload
    ctx = {
        "plan": plan,
        "banks": [b for b in banks if isinstance(b, dict)],
        "from_date": from_date,
        "to_date": to_date,
        "today": date.today().isoformat(),
        "error": error,
    }
    return _TEMPLATES.TemplateResponse(request, "bas/settle.html", ctx)


@router.post("/bas/settle", response_model=None)
async def bas_settle(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body: dict = {
        "from_date": str(form.get("from_date", "")),
        "to_date": str(form.get("to_date", "")),
        "settlement_date": str(form.get("settlement_date", "") or date.today().isoformat()),
    }
    if str(form.get("pay_now", "")) in ("1", "on", "true") and form.get("pay_from_account_id"):
        body["pay_from_account_id"] = str(form.get("pay_from_account_id"))
        if form.get("payment_date"):
            body["payment_date"] = str(form.get("payment_date"))
    async with api_client(request) as client:
        resp = await client.post("/api/v1/bas/settle", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        out = resp.json()
        msg = _("BAS settled (journal %(ref)s).") % {"ref": out.get("settlement_ref") or ""}
        if out.get("payment_ref"):
            msg += " " + _("Payment to the ATO recorded (journal %(ref)s).") % {"ref": out["payment_ref"]}
        request.session["flash"] = msg
        return RedirectResponse(url="/bas", status_code=303)
    request.session["flash"] = _("The BAS could not be settled: %(why)s") % {"why": _detail(resp)}
    qs = urlencode({"from_date": body["from_date"], "to_date": body["to_date"]})
    return RedirectResponse(url=f"/bas/settle?{qs}", status_code=303)
