"""Bank feeds screen — batch 4 (P1-19), 2026-11-13.

GET  /banking/feeds                       — connections, status, last sync
POST /banking/feeds/connect               — start a connection (consent flow)
POST /banking/feeds/{connection_id}/sync  — fetch new transactions now
POST /banking/feeds/{connection_id}/disconnect

The engine talks to a bank-feed relay. Until a production feed provider is
contracted the relay runs in test mode: connecting and syncing work end to
end but no real bank data arrives. The page says so plainly instead of
pretending. When the feature is not part of the plan the engine answers 404
and the page explains that, pointing at statement import instead.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _detail(resp, fallback: str) -> str:
    try:
        d = resp.json().get("detail", fallback)
    except Exception:
        return fallback
    return d if isinstance(d, str) else fallback


@router.get("/banking/feeds", response_class=HTMLResponse, response_model=None)
async def bank_feeds_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    connections: list[dict] = []
    accounts: list[dict] = []
    available = True
    error: str | None = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/bank-feeds/connections")
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.status_code in (403, 404):
                available = False
            elif resp.is_success:
                payload = resp.json()
                connections = payload if isinstance(payload, list) else payload.get("items", [])
            else:
                error = _detail(resp, _("Bank feeds could not be loaded (HTTP %(code)s).") % {"code": resp.status_code})
            acct = await client.get("/api/v1/reconciliation/accounts")
            if acct.is_success:
                payload = acct.json()
                accounts = payload if isinstance(payload, list) else payload.get("items", [])
    except ModuleUnavailable:
        degraded = True
    names = {a["id"]: f"{a.get('code', '')} {a.get('name', '')}".strip() for a in accounts}
    return _TEMPLATES.TemplateResponse(
        request,
        "bank_feeds/index.html",
        {
            "connections": connections,
            "accounts": accounts,
            "names": names,
            "available": available,
            "error": error,
            "degraded": degraded,
            "flash": request.session.pop("flash", None),
        },
    )


async def _post(request: Request, path: str, body: dict | None, method: str = "post") -> object:
    async with api_client(request) as client:
        if method == "delete":
            return await client.delete(path)
        return await client.post(path, json=body)


@router.post("/banking/feeds/connect", response_model=None)
async def bank_feeds_connect(request: Request) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    bank = str(form.get("bank", "")).strip()
    account_id = str(form.get("account_id", "")).strip()
    label = str(form.get("account_label", "")).strip() or bank
    if not bank or not account_id:
        request.session["flash"] = _("Choose the bank and the account its lines go into.")
        return RedirectResponse(url="/banking/feeds", status_code=303)
    try:
        resp = await _post(
            request, "/api/v1/bank-feeds/connections",
            {"bank": bank, "account_label": label[:200], "ledger_id": account_id},
        )
    except ModuleUnavailable:
        request.session["flash"] = _("The accounting engine could not be reached — nothing was changed. Try again in a moment.")
        return RedirectResponse(url="/banking/feeds", status_code=303)
    if getattr(resp, "is_success", False):
        body = resp.json()  # type: ignore[attr-defined]
        if body.get("consent_url"):
            return RedirectResponse(url=body["consent_url"], status_code=303)
        request.session["flash"] = _(
            "Connection recorded. The bank feed service is in test mode, so no "
            "bank approval page was opened and no real transactions will arrive yet."
        )
    else:
        request.session["flash"] = _detail(resp, _("Could not connect (HTTP %(code)s).") % {"code": getattr(resp, "status_code", "?")})
    return RedirectResponse(url="/banking/feeds", status_code=303)


@router.post("/banking/feeds/{connection_id}/sync", response_model=None)
async def bank_feeds_sync(request: Request, connection_id: str) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    try:
        resp = await _post(request, "/api/v1/bank-feeds/sync", {"connection_id": connection_id})
    except ModuleUnavailable:
        request.session["flash"] = _("The accounting engine could not be reached — nothing was changed. Try again in a moment.")
        return RedirectResponse(url="/banking/feeds", status_code=303)
    if getattr(resp, "is_success", False):
        body = resp.json()  # type: ignore[attr-defined]
        if body.get("stub"):
            request.session["flash"] = _(
                "Sync ran in test mode: the bank feed service is not connected to a "
                "live provider yet, so no transactions were fetched. Import a statement file instead."
            )
        else:
            request.session["flash"] = _("Sync complete — %(n)s new bank line(s).") % {"n": body.get("inserted", 0)}
    else:
        request.session["flash"] = _detail(resp, _("Sync failed (HTTP %(code)s).") % {"code": getattr(resp, "status_code", "?")})
    return RedirectResponse(url="/banking/feeds", status_code=303)


@router.post("/banking/feeds/{connection_id}/disconnect", response_model=None)
async def bank_feeds_disconnect(request: Request, connection_id: str) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    try:
        resp = await _post(request, f"/api/v1/bank-feeds/connections/{connection_id}", None, "delete")
    except ModuleUnavailable:
        request.session["flash"] = _("The accounting engine could not be reached — nothing was changed. Try again in a moment.")
        return RedirectResponse(url="/banking/feeds", status_code=303)
    request.session["flash"] = (
        _("Bank feed disconnected.") if getattr(resp, "is_success", False)
        else _detail(resp, _("Could not disconnect (HTTP %(code)s).") % {"code": getattr(resp, "status_code", "?")})
    )
    return RedirectResponse(url="/banking/feeds", status_code=303)
