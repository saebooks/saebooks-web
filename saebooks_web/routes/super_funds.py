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


# ---------------------------------------------------------------------------
# Bulk action — POST /super-funds/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_SUPER_FUNDS = {
    "archive": ("DELETE", "/api/v1/super-funds/{id}"),
}


@router.post("/super-funds/bulk", response_class=HTMLResponse, response_model=None)
async def super_funds_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many super funds at once.

    Form fields:
      action  — one of: archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /super-funds. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_SUPER_FUNDS:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/super-funds", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/super-funds", status_code=303)

    method, path_tpl = _BULK_ACTIONS_SUPER_FUNDS[action]
    ok = 0
    failed: list[str] = []
    async with api_client(request) as client:
        for row_id in ids:
            try:
                resp = await client.request(method, path_tpl.format(id=row_id))
                if 200 <= resp.status_code < 300:
                    ok += 1
                else:
                    msg = ""
                    try:
                        body = resp.json()
                        detail = body.get("detail")
                        if isinstance(detail, str):
                            msg = detail
                        elif isinstance(detail, list) and detail:
                            msg = detail[0].get("msg", str(detail))
                    except Exception:
                        msg = ""
                    failed.append(f"{row_id[:8]} ({resp.status_code}{': ' + msg if msg else ''})")
            except Exception as exc:
                failed.append(f"{row_id[:8]} (transport error: {exc!s})")

    label = action.replace("_", " ").title()
    if failed:
        request.session["flash"] = (
            f"{label}: {ok} succeeded, {len(failed)} failed — " + "; ".join(failed[:5])
            + (f" … +{len(failed) - 5} more" if len(failed) > 5 else "")
        )
    else:
        request.session["flash"] = f"{label}: {ok} super fund{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/super-funds", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/super-funds/{fund_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def super_fund_hard_delete(request: Request, fund_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/super-funds",
        entity_id=fund_id,
        entity_label=f"Super fund {fund_id}",
        list_url="/super-funds",
        detail_url=f"/super-funds/{fund_id}",
    )
