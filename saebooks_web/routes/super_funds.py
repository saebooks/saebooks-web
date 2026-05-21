"""Super-fund web routes — list / create / detail / edit / set-default / archive.

Routes:

  GET  /super-funds               — list (default fund first)
  GET  /super-funds/new           — create form (APRA / SMSF toggle)
  POST /super-funds/new           — submit create
  GET  /super-funds/{id}          — detail
  GET  /super-funds/{id}/edit     — edit form
  POST /super-funds/{id}/edit     — submit edit with If-Match
  POST /super-funds/{id}/set-default — mark as default
  POST /super-funds/{id}/archive  — soft-delete
"""
from __future__ import annotations

import uuid
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
# List
# ---------------------------------------------------------------------------


@router.get("/super-funds", response_class=HTMLResponse, response_model=None)
async def super_funds_list(
    request: Request,
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    funds: list[dict] = []
    total = 0
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/api/v1/super-funds", params={"limit": limit, "offset": offset})
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            funds = payload.get("items", [])
            total = payload.get("total", len(funds))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Sort default first, then alphabetically.
    funds = sorted(funds, key=lambda f: (not f.get("is_default"), f.get("name", "").lower()))

    return _TEMPLATES.TemplateResponse(
        request,
        "super_funds/list.html",
        {
            "funds": funds,
            "total": total,
            "error": error,
            "limit": limit,
            "offset": offset,
            "prev_offset": max(offset - limit, 0) if offset > 0 else None,
            "next_offset": offset + limit if (offset + limit) < total else None,
        },
    )


# ---------------------------------------------------------------------------
# New
# ---------------------------------------------------------------------------


@router.get("/super-funds/new", response_class=HTMLResponse, response_model=None)
async def super_fund_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "super_funds/new.html",
        {
            "form": {
                "name": "",
                "is_smsf": False,
                "usi": "",
                "employer_abn": "",
                "esa": "",
                "smsf_bsb": "",
                "smsf_account_number": "",
                "smsf_account_name": "",
                "is_default": False,
            },
            "errors": {},
        },
    )


@router.post("/super-funds/new", response_class=HTMLResponse, response_model=None)
async def super_fund_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    is_smsf = form.get("is_smsf") in ("on", "true", "1")

    payload: dict[str, object] = {
        "name": form.get("name", "").strip(),
        "is_smsf": is_smsf,
        "is_default": form.get("is_default") in ("on", "true", "1"),
    }

    if is_smsf:
        for field in ("employer_abn", "esa", "smsf_bsb", "smsf_account_number", "smsf_account_name"):
            if val := form.get(field, "").strip():
                payload[field] = val
    else:
        if usi := form.get("usi", "").strip():
            payload["usi"] = usi

    async with api_client(request) as client:
        resp = await client.post("/api/v1/super-funds", json=payload)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code in (200, 201):
            fund_id = resp.json()["id"]
            return RedirectResponse(url=f"/super-funds/{fund_id}", status_code=303)

        errors: dict[str, str] = {}
        try:
            err_body = resp.json()
            errors["_global"] = err_body.get("detail") or f"HTTP {resp.status_code}"
        except Exception:
            errors["_global"] = f"HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "super_funds/new.html",
        {"form": form, "errors": errors},
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get(
    "/super-funds/{fund_id}",
    response_class=HTMLResponse,
    response_model=None,
)
async def super_fund_detail(
    fund_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    fund: dict | None = None
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/super-funds/{fund_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            fund = resp.json()
        else:
            error = f"API error: HTTP {resp.status_code}"

    if fund is None:
        return _TEMPLATES.TemplateResponse(
            request,
            "super_funds/detail.html",
            {"fund": None, "error": error},
            status_code=404,
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "super_funds/detail.html",
        {"fund": fund, "error": error},
    )


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@router.get(
    "/super-funds/{fund_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def super_fund_edit_form(
    fund_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    fund: dict | None = None

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/super-funds/{fund_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            fund = resp.json()

    if fund is None:
        return RedirectResponse(url="/super-funds", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "super_funds/edit.html",
        {
            "fund": fund,
            "form": fund,
            "errors": {},
        },
    )


@router.post(
    "/super-funds/{fund_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def super_fund_edit_submit(
    fund_id: uuid.UUID, request: Request
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    payload: dict[str, object] = {}

    if "name" in form_data:
        payload["name"] = form.get("name", "").strip() or None
    for field in ("usi", "employer_abn", "esa", "smsf_bsb", "smsf_account_number", "smsf_account_name"):
        if field in form_data:
            payload[field] = form.get(field, "").strip() or None

    headers: dict[str, str] = {}
    if version := form.get("version", "").strip():
        headers["If-Match"] = version

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/super-funds/{fund_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            return RedirectResponse(url=f"/super-funds/{fund_id}", status_code=303)

        errors: dict[str, str] = {}
        try:
            errors["_global"] = resp.json().get("detail") or f"HTTP {resp.status_code}"
        except Exception:
            errors["_global"] = f"HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "super_funds/edit.html",
        {
            "fund": {"id": str(fund_id), **form},
            "form": form,
            "errors": errors,
        },
    )


# ---------------------------------------------------------------------------
# Set default
# ---------------------------------------------------------------------------


@router.post(
    "/super-funds/{fund_id}/set-default",
    response_class=HTMLResponse,
    response_model=None,
)
async def super_fund_set_default(
    fund_id: uuid.UUID, request: Request
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.post(f"/api/v1/super-funds/{fund_id}/set-default")
    return RedirectResponse(url=f"/super-funds/{fund_id}", status_code=303)


# ---------------------------------------------------------------------------
# Archive (soft-delete)
# ---------------------------------------------------------------------------


@router.post(
    "/super-funds/{fund_id}/archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def super_fund_archive(
    fund_id: uuid.UUID, request: Request
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        await client.delete(f"/api/v1/super-funds/{fund_id}")
    return RedirectResponse(url="/super-funds", status_code=303)
