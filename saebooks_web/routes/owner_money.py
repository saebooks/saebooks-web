"""Owner money screens — P1-47, batch 14.

GET  /owner-money                 — what the business owes the owner: expenses they paid
                                    personally, with "Reimburse selected"
POST /owner-money/reimburse       — pay the ticked expenses back with one transfer
GET  /owner-money/report          — owner (or director) loan balance, line by line
GET  /owner-money/settings        — which accounts hold owner loan and drawings
POST /owner-money/settings        — save them
POST /owner-money/setup-accounts  — create the standard accounts that are missing

Engine: ``/api/v1/owner_money`` (``saebooks/services/owner_money.py``).
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
        return str(detail[0].get("msg"))
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


@router.get("/owner-money", response_class=HTMLResponse, response_model=None)
async def owner_money_home(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    error = None
    owing: dict = {"items": [], "total": "0", "count": 0}
    settings: dict = {}
    balance: dict | None = None
    async with api_client(request) as client:
        resp = await client.get("/api/v1/owner_money/reimbursements")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            owing = resp.json()
        else:
            error = _detail(resp)
        s_resp = await client.get("/api/v1/owner_money/settings")
        if s_resp.is_success:
            settings = s_resp.json()
        if settings.get("owner_loan_account"):
            b_resp = await client.get("/api/v1/owner_money/owner_loan_report")
            if b_resp.is_success:
                balance = b_resp.json()
        banks = await _accounts(client, "ASSET")
    return _TEMPLATES.TemplateResponse(
        request,
        "owner_money/index.html",
        {
            "owing": owing,
            "settings": settings,
            "balance": balance,
            "banks": banks,
            "error": error,
            "flash": request.session.pop("flash", None),
            "today": date.today().isoformat(),
            "idempotency_key": str(uuid.uuid4()),
        },
    )


@router.post("/owner-money/reimburse", response_model=None)
async def owner_money_reimburse(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    expense_ids = [str(v) for v in form.getlist("expense_ids") if str(v).strip()]
    if not expense_ids:
        request.session["flash"] = "Tick at least one expense to pay back."
        return RedirectResponse(url="/owner-money", status_code=303)
    payload = {
        "expense_ids": expense_ids,
        "from_account_id": str(form.get("from_account_id", "")).strip(),
        "reimbursement_date": str(form.get("reimbursement_date", "")).strip()
        or date.today().isoformat(),
    }
    ref = str(form.get("reference", "")).strip()
    if ref:
        payload["reference"] = ref
    if not payload["from_account_id"]:
        request.session["flash"] = "Choose the account the money is paid from."
        return RedirectResponse(url="/owner-money", status_code=303)
    key = str(form.get("idempotency_key", "")).strip() or str(uuid.uuid4())
    async with api_client(request) as client:
        for _attempt in range(4):
            resp = await client.post(
                "/api/v1/owner_money/reimburse",
                json=payload,
                headers={"X-Idempotency-Key": key},
            )
            if resp.status_code != 503:
                break
            await asyncio.sleep(0.5)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        body = resp.json()
        n = len(body.get("expense_ids", []))
        request.session["flash"] = (
            f"Paid back {body.get('amount')} for {n} expense{'s' if n != 1 else ''}."
        )
        return RedirectResponse(url=f"/transfers/{body['transfer_id']}", status_code=303)
    request.session["flash"] = _detail(resp)
    return RedirectResponse(url="/owner-money", status_code=303)


@router.get("/owner-money/report", response_class=HTMLResponse, response_model=None)
async def owner_money_report(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    params = {}
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    async with api_client(request) as client:
        resp = await client.get("/api/v1/owner_money/owner_loan_report", params=params)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "owner_money/report.html",
        {
            "report": resp.json() if resp.is_success else None,
            "error": None if resp.is_success else _detail(resp),
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    )


@router.get("/owner-money/settings", response_class=HTMLResponse, response_model=None)
async def owner_money_settings(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get("/api/v1/owner_money/settings")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        owner_options = await _accounts(client, "LIABILITY", "EQUITY")
    return _TEMPLATES.TemplateResponse(
        request,
        "owner_money/settings.html",
        {
            "settings": resp.json() if resp.is_success else {},
            "error": None if resp.is_success else _detail(resp),
            "owner_options": owner_options,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/owner-money/settings", response_model=None)
async def owner_money_settings_save(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    payload = {}
    for field in ("owner_loan_account_id", "drawings_account_id"):
        val = str(form.get(field, "")).strip()
        if val:
            payload[field] = val
    async with api_client(request) as client:
        resp = await client.put("/api/v1/owner_money/settings", json=payload)
    request.session["flash"] = "Saved." if resp.is_success else _detail(resp)
    return RedirectResponse(url="/owner-money/settings", status_code=303)


@router.post("/owner-money/setup-accounts", response_model=None)
async def owner_money_setup_accounts(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.post("/api/v1/owner_money/setup_accounts", json={})
    if resp.is_success:
        created = resp.json().get("created", [])
        request.session["flash"] = (
            f"Created {len(created)} account{'s' if len(created) != 1 else ''}: {', '.join(created)}."
            if created else "All the standard accounts already exist."
        )
    else:
        request.session["flash"] = _detail(resp)
    return RedirectResponse(url="/owner-money/settings", status_code=303)
