"""Foreign currency screens (batch 10).

* ``GET  /fx/lookup``                       — JSON: the rate for a currency on a
  date and where it came from. Used by the currency fields on invoices,
  bills, expenses and payments to prefill the rate and show its source.
* ``GET  /settings/exchange-rates``         — the rate screen: the company's
  own rates, a lookup, and a button to fetch the latest Reserve Bank rates.
* ``POST /settings/exchange-rates``         — save a rate.
* ``POST /settings/exchange-rates/{id}/delete``
* ``POST /settings/exchange-rates/refresh`` — download the latest RBA rates.
* ``POST /reports/fx-revaluation/post``     — post the month-end revaluation.

``currency_context`` is shared with the document routes: it returns the
company currency and the currency list for the picker, and whether
foreign currency is switched on for this company's plan.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.fx_context import fx_context
from saebooks_web.i18n import gettext as _

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _require_admin(request: Request) -> bool:
    role = request.session.get("user_role", "")
    return bool(request.session.get("is_sae_staff")) or role in ("owner", "admin")


def _detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:
        return _("Something went wrong (HTTP %(code)s).") % {"code": resp.status_code}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            return str(first.get("msg", first))
    return _("Something went wrong (HTTP %(code)s).") % {"code": resp.status_code}


def currency_context(request: Request) -> dict[str, Any]:
    """Company currency + picker list (see ``saebooks_web.fx_context``)."""
    return fx_context(request)


# ---------------------------------------------------------------------------
# JSON lookup for the currency fields
# ---------------------------------------------------------------------------


@router.get("/fx/lookup", response_model=None)
async def fx_lookup(
    request: Request, currency: str = "", on: str | None = None
) -> JSONResponse:
    if not _require_auth(request):
        return JSONResponse({"error": _("Please sign in again.")}, status_code=401)
    ccy = currency.strip().upper()
    if len(ccy) != 3:
        return JSONResponse({"error": _("Choose a currency.")}, status_code=422)
    params = {"currency": ccy}
    if on:
        params["date"] = on
    async with api_client(request) as client:
        resp = await client.get("/api/v1/fx/rate", params=params)
    if resp.is_success:
        body = resp.json()
        return JSONResponse(
            {
                "rate": body.get("rate"),
                "source": body.get("source"),
                "source_label": body.get("source_label"),
                "rate_date": body.get("rate_date"),
                "base_currency": body.get("base_currency"),
            }
        )
    return JSONResponse({"error": _detail(resp)}, status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Rate screen
# ---------------------------------------------------------------------------


@router.get("/settings/exchange-rates", response_class=HTMLResponse, response_model=None)
async def exchange_rates_page(
    request: Request, currency: str = "", on: str = ""
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    rates: list[dict] = []
    error: str | None = None
    lookup: dict | None = None
    lookup_error: str | None = None
    async with api_client(request) as client:
        fx = currency_context(request)
        resp = await client.get("/api/v1/fx/rates", params={"limit": 300})
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            rates = resp.json().get("items", [])
        elif resp.status_code == 404:
            error = _("Foreign currency is not part of this company's plan.")
        else:
            error = _detail(resp)
        if currency:
            look = await client.get(
                "/api/v1/fx/rate",
                params={"currency": currency.upper(), "date": on or date.today().isoformat()},
            )
            if look.is_success:
                lookup = look.json()
            else:
                lookup_error = _detail(look)

    return _TEMPLATES.TemplateResponse(
        request,
        "settings/exchange_rates.html",
        {
            "rates": rates,
            "fx": fx,
            "error": error,
            "flash": request.session.pop("flash", None),
            "flash_error": request.session.pop("flash_error", None),
            "lookup": lookup,
            "lookup_error": lookup_error,
            "lookup_currency": currency.upper(),
            "lookup_on": on or date.today().isoformat(),
            "today": date.today().isoformat(),
            "can_edit": _require_admin(request),
            "mode": getattr(request.state, "active_company_bookkeeping_mode", "full") or "full",
        },
    )


@router.post("/settings/exchange-rates", response_class=HTMLResponse, response_model=None)
async def exchange_rate_save(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    payload = {
        "currency": str(form.get("currency", "")).strip().upper(),
        "rate_date": str(form.get("rate_date", "")).strip(),
        "rate": str(form.get("rate", "")).strip(),
        "note": str(form.get("note", "")).strip() or None,
    }
    if not payload["currency"] or not payload["rate_date"] or not payload["rate"]:
        request.session["flash_error"] = _("Currency, date and rate are all needed.")
        return RedirectResponse(url="/settings/exchange-rates", status_code=303)
    async with api_client(request) as client:
        resp = await client.post("/api/v1/fx/rates", json=payload)
    if resp.is_success:
        request.session["flash"] = _("Rate saved: 1 %(ccy)s = %(rate)s on %(date)s.") % {
            "ccy": payload["currency"],
            "rate": payload["rate"],
            "date": payload["rate_date"],
        }
    else:
        request.session["flash_error"] = _detail(resp)
    return RedirectResponse(url="/settings/exchange-rates", status_code=303)


@router.post(
    "/settings/exchange-rates/{rate_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def exchange_rate_delete(request: Request, rate_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/fx/rates/{rate_id}")
    if resp.is_success:
        request.session["flash"] = _("Rate removed.")
    else:
        request.session["flash_error"] = _detail(resp)
    return RedirectResponse(url="/settings/exchange-rates", status_code=303)


@router.post(
    "/settings/exchange-rates/refresh", response_class=HTMLResponse, response_model=None
)
async def exchange_rates_refresh(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.post("/api/v1/fx/rates/refresh")
    if resp.is_success:
        body = resp.json()
        request.session["flash"] = _(
            "Reserve Bank rates are up to date to %(date)s."
        ) % {"date": body.get("latest_date") or "—"}
    else:
        request.session["flash_error"] = _detail(resp)
    return RedirectResponse(url="/settings/exchange-rates", status_code=303)


# ---------------------------------------------------------------------------
# Month-end revaluation (P1-51)
# ---------------------------------------------------------------------------


@router.post("/reports/fx-revaluation/post", response_class=HTMLResponse, response_model=None)
async def fx_revaluation_post(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    through = str(form.get("through_date", "")).strip()
    if not through:
        request.session["flash_error"] = _("Choose the revaluation date.")
        return RedirectResponse(url="/reports/fx-revaluation", status_code=303)
    async with api_client(request) as client:
        resp = await client.post("/api/v1/fx/revaluation", json={"through_date": through})
    if resp.is_success:
        body = resp.json()
        posted = len(body.get("posted", []))
        skipped = body.get("skipped_currencies", [])
        if posted:
            request.session["flash"] = _(
                "Revaluation posted for %(n)s currency(ies) on %(date)s, reversed the next day."
            ) % {"n": posted, "date": through}
        elif skipped:
            request.session["flash"] = _(
                "Already posted for %(date)s: %(ccys)s. Nothing new to post."
            ) % {"date": through, "ccys": ", ".join(skipped)}
        else:
            request.session["flash"] = _("Nothing to revalue on %(date)s.") % {"date": through}
    else:
        request.session["flash_error"] = _detail(resp)
    return RedirectResponse(url=f"/reports/fx-revaluation?as_of_date={through}", status_code=303)
