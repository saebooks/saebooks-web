"""Company list and management views.

GET  /companies            -- list all companies for the tenant
GET  /companies/new        -- new company form (admin only)
POST /companies            -- create a company (proxies POST /api/v1/companies)
GET  /settings/companies   -- redirect to /companies
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

_TOP_FIELDS = ("name", "legal_name", "trading_name", "abn")


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _require_admin(request: Request) -> bool:
    role = request.session.get("user_role", "")
    is_staff = bool(request.session.get("is_sae_staff"))
    return is_staff or role == "admin"


# ---------------------------------------------------------------------------
# GET /settings/companies — canonical redirect
# ---------------------------------------------------------------------------


@router.get("/settings/companies", response_class=HTMLResponse, response_model=None)
async def settings_companies_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/companies", status_code=302)


# ---------------------------------------------------------------------------
# GET /companies — list
# ---------------------------------------------------------------------------


@router.get("/companies", response_class=HTMLResponse, response_model=None)
async def companies_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    companies: list[dict] = []
    total: int = 0
    multi_company_enabled: bool = False
    error: str | None = None

    async with api_client(request) as client:
        resp_list = await client.get("/api/v1/companies", params={"limit": 100, "offset": 0})
        resp_lic = await client.get("/api/v1/license")

    if resp_list.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp_list.is_success:
        data = resp_list.json()
        companies = data.get("items", [])
        total = data.get("total", len(companies))
    else:
        error = f"API error: HTTP {resp_list.status_code}"

    if resp_lic.is_success:
        flags = resp_lic.json().get("flags", {})
        multi_company_enabled = bool(flags.get("multi_company", False))

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "companies/list.html",
        {
            "companies": companies,
            "total": total,
            "multi_company_enabled": multi_company_enabled,
            "is_admin": _require_admin(request),
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# GET /companies/new — create form
# ---------------------------------------------------------------------------


@router.get("/companies/new", response_class=HTMLResponse, response_model=None)
async def companies_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    return _TEMPLATES.TemplateResponse(
        request,
        "companies/new.html",
        {"form": {}, "errors": {}},
    )


# ---------------------------------------------------------------------------
# POST /companies — create
# ---------------------------------------------------------------------------


@router.post("/companies", response_class=HTMLResponse, response_model=None)
async def companies_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    payload: dict[str, object] = {}
    for field in _TOP_FIELDS:
        val = form.get(field, "").strip() or None
        if val is not None:
            payload[field] = val

    async with api_client(request) as client:
        resp = await client.post("/api/v1/companies", json=payload)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        request.session["flash"] = "Company created."
        return RedirectResponse(url="/companies", status_code=303)

    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "companies/new.html",
            {
                "form": form,
                "errors": {
                    "__all__": "Multi-company management requires a Business or higher edition."
                },
            },
            status_code=403,
        )

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    parts = [p for p in loc if p != "body"]
                    key = str(parts[0]) if parts else "__all__"
                    errors[key] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        try:
            detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        except Exception:
            detail = f"API error: HTTP {resp.status_code}"
        errors["__all__"] = str(detail)

    return _TEMPLATES.TemplateResponse(
        request,
        "companies/new.html",
        {"form": form, "errors": errors},
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /companies/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_COMPANIES = {
    "archive": ("DELETE", "/api/v1/companies/{id}"),
}


@router.post("/companies/bulk", response_class=HTMLResponse, response_model=None)
async def companies_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many companys at once.

    Form fields:
      action  — one of: archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /companies. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_COMPANIES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/companies", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/companies", status_code=303)

    method, path_tpl = _BULK_ACTIONS_COMPANIES[action]
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
        request.session["flash"] = f"{label}: {ok} company{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/companies", status_code=303)
