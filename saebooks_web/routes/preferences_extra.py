"""Preferences that save (gap P2-9, 2026-11-13 sweep).

The Preferences page was read-only apart from the language. This adds:

* ``POST /settings/preferences/display`` — how dates and numbers are shown,
  a per-device choice kept in two year-long cookies like the language:
  ``sb_date_format`` (``iso`` 2026-11-13, ``dmy`` 13/11/2026, ``long``
  13 Nov 2026 — applied in the browser by static/js/phone.js) and
  ``sb_num_group`` (``1`` = always show thousands separators — applied on
  the server by ``i18n.format.force_grouping``).
* ``GET  /settings/preferences/timezone`` — HTMX card showing the company's
  time zone (gap P2-7: "today" for report dates and overdue counts).
* ``POST /settings/preferences/timezone`` — save it on the company through
  the engine (owners and admins only; it changes what every user sees).
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

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)

_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
DATE_FORMATS = ("iso", "dmy", "long")

#: Offered first; any other IANA name can be typed. Mirrors the engine's
#: company_clock.COMMON_ZONES.
COMMON_ZONES = (
    ("Australia/Brisbane", "Brisbane (Queensland, no daylight saving)"),
    ("Australia/Sydney", "Sydney, Canberra (New South Wales, ACT)"),
    ("Australia/Melbourne", "Melbourne (Victoria)"),
    ("Australia/Hobart", "Hobart (Tasmania)"),
    ("Australia/Adelaide", "Adelaide (South Australia)"),
    ("Australia/Darwin", "Darwin (Northern Territory)"),
    ("Australia/Perth", "Perth (Western Australia)"),
    ("Pacific/Auckland", "Auckland (New Zealand)"),
    ("Europe/Tallinn", "Tallinn (Estonia)"),
)


def _is_admin(request: Request) -> bool:
    role = request.session.get("user_role", "")
    return bool(request.session.get("is_sae_staff")) or role in ("owner", "admin")


@router.post("/settings/preferences/display", response_model=None)
async def save_display(request: Request) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    date_format = str(form.get("date_format") or "iso")
    if date_format not in DATE_FORMATS:
        date_format = "iso"
    group = "1" if form.get("num_group") in ("1", "on", "true") else "0"
    request.session["flash"] = _("Display preferences saved on this device.")
    resp = RedirectResponse(url="/settings/preferences", status_code=303)
    for name, value in (("sb_date_format", date_format), ("sb_num_group", group)):
        resp.set_cookie(
            name, value, max_age=_COOKIE_MAX_AGE, httponly=False, samesite="lax",
            secure=request.url.scheme == "https",
        )
    return resp


async def _company(client, request: Request) -> dict | None:
    company_id = getattr(request.state, "active_company_id", None)
    if company_id:
        resp = await client.get(f"/api/v1/companies/{company_id}")
    else:
        resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if resp.is_success:
            items = resp.json().get("items") or []
            return items[0] if items else None
        return None
    return resp.json() if resp.is_success else None


@router.get("/settings/preferences/timezone", response_class=HTMLResponse, response_model=None)
async def timezone_card(request: Request) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    company: dict | None = None
    try:
        async with api_client(request) as client:
            company = await _company(client, request)
    except ModuleUnavailable:
        company = None
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/timezone_card.html",
        {
            "company": company,
            "zones": COMMON_ZONES,
            "can_edit": _is_admin(request),
            "error": request.session.pop("tz_error", None),
        },
    )


@router.post("/settings/preferences/timezone", response_model=None)
async def save_timezone(request: Request) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    back = RedirectResponse(url="/settings/preferences#timezone", status_code=303)
    if not _is_admin(request):
        request.session["tz_error"] = _("Only an owner or admin can change the company's time zone.")
        return back
    form = await request.form()
    zone = str(form.get("timezone_other") or "").strip() or str(form.get("timezone") or "").strip()
    async with api_client(request) as client:
        company = await _company(client, request)
        if company is None:
            request.session["tz_error"] = _("The company could not be loaded.")
            return back
        resp = await client.patch(
            f"/api/v1/companies/{company['id']}",
            json={"timezone": zone},
            headers={"If-Match": str(company.get("version", ""))},
        )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        request.session["flash"] = _("Time zone saved.")
    else:
        detail = None
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        request.session["tz_error"] = (
            detail if isinstance(detail, str) else _("Could not save the time zone (HTTP %(code)s).") % {"code": resp.status_code}
        )
    return back
