"""Prorate previews — interactive calculator UI.

GET  /proration                       — three-tab calculator page
POST /proration/preview               — Prorate #3 generic
POST /proration/first-period-preview  — Prorate #1 first-period
POST /proration/plan-change-preview   — Prorate #2 mid-period change

POSTs return an HTMX fragment (`_result_*.html`) when called via HTMX,
or redirect back to /proration with a flash on full-page submit.

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get("/proration", response_class=HTMLResponse, response_model=None)
async def proration_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "proration/index.html",
        {"flash": flash, "result": None, "active_tab": "preview"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _preview_call(request: Request, endpoint: str, payload: dict) -> tuple[int, dict | str]:
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/proration/{endpoint}", json=payload)
    if resp.status_code == 401:
        request.session.clear()
        return 401, ""
    if resp.is_success:
        return resp.status_code, resp.json()
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


# ---------------------------------------------------------------------------
# POST handlers (HTMX result fragments)
# ---------------------------------------------------------------------------


@router.post("/proration/preview", response_class=HTMLResponse, response_model=None)
async def proration_preview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    payload = {
        "full_period_amount": (form.get("full_period_amount") or "").strip(),
        "basis": (form.get("basis") or "MONTHLY").strip(),
        "service_start": (form.get("service_start") or "").strip(),
        "service_end": (form.get("service_end") or "").strip(),
    }
    status, body = await _preview_call(request, "preview", payload)
    if status == 401:
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "proration/_result_preview.html",
        {"status": status, "result": body if isinstance(body, dict) else None,
         "error": body if not isinstance(body, dict) else None,
         "form": dict(payload)},
    )


@router.post(
    "/proration/first-period-preview",
    response_class=HTMLResponse,
    response_model=None,
)
async def proration_first_period(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    payload = {
        "full_period_amount": (form.get("full_period_amount") or "").strip(),
        "basis": (form.get("basis") or "MONTHLY").strip(),
        "service_start": (form.get("service_start") or "").strip(),
        "service_end": (form.get("service_end") or "").strip(),
    }
    status, body = await _preview_call(request, "first-period-preview", payload)
    if status == 401:
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "proration/_result_first_period.html",
        {"status": status, "result": body if isinstance(body, dict) else None,
         "error": body if not isinstance(body, dict) else None,
         "form": dict(payload)},
    )


@router.post(
    "/proration/plan-change-preview",
    response_class=HTMLResponse,
    response_model=None,
)
async def proration_plan_change(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    payload = {
        "old_period_amount": (form.get("old_period_amount") or "").strip(),
        "new_period_amount": (form.get("new_period_amount") or "").strip(),
        "period_start": (form.get("period_start") or "").strip(),
        "period_end": (form.get("period_end") or "").strip(),
        "change_date": (form.get("change_date") or "").strip(),
    }
    status, body = await _preview_call(request, "plan-change-preview", payload)
    if status == 401:
        return RedirectResponse(url="/login", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "proration/_result_plan_change.html",
        {"status": status, "result": body if isinstance(body, dict) else None,
         "error": body if not isinstance(body, dict) else None,
         "form": dict(payload)},
    )
