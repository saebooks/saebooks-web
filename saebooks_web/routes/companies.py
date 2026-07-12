"""Company list and management views.

GET  /companies            -- list all companies for the tenant
GET  /companies/new        -- new company form (admin only)
POST /companies            -- create a company (proxies POST /api/v1/companies)
GET  /settings/companies   -- redirect to /companies
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_TOP_FIELDS = ("name", "legal_name", "trading_name", "abn")

# EE onboarding (P3). registrikood = 8-digit Estonian business registry
# code; kmv = Estonian VAT number, "EE" + 9 digits, optional.
_REGISTRIKOOD_RE = re.compile(r"^\d{8}$")
_KMV_RE = re.compile(r"^EE\d{9}$")

# Prerequisite gap verified against the engine (saebooks/api/v1/schemas.py,
# CompanyCreate): jurisdiction/registrikood/kmv/coa_template_key are not
# accepted there today — registrikood/kmv have no Company column at all,
# jurisdiction and coa_template_key exist as columns but aren't exposed on
# CompanyCreate, and services/templates.py's "ee/default" applier is a
# NotImplementedError stub ("M3"). Sent for real below (nothing is faked
# client-side) so the wiring is ready the day the engine lands this, but
# until then the engine's pydantic model silently drops these extra keys
# (no ``extra="forbid"`` set) — so the flash set in ``companies_create``
# below is the honest signal that replaces the silence, not an optional
# nicety. Built with ``_()`` INSIDE the route (not as a module-level
# constant) — ``gettext`` resolves the active request's locale from a
# contextvar at call time; freezing it at import time would defeat that
# and always serve the process-start locale to every user.


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

    # Jurisdiction axis (P3). Defaults AU so an unset/blank/unknown value
    # behaves exactly like the pre-P3 form — no jurisdiction key at all
    # goes into the payload, keeping the AU path's request body identical
    # to before this packet.
    jurisdiction = (form.get("jurisdiction", "AU") or "AU").strip().upper()

    payload: dict[str, object] = {}
    for field in _TOP_FIELDS:
        val = form.get(field, "").strip() or None
        if val is not None:
            payload[field] = val

    if jurisdiction == "EE":
        # AU's abn field is meaningless for an EE company — drop it even
        # if a stale value survived a form re-render after switching
        # jurisdiction client-side.
        payload.pop("abn", None)

        registrikood = form.get("registrikood", "").strip()
        kmv_raw = form.get("kmv", "").strip().upper()
        base_currency = (form.get("base_currency", "").strip() or "EUR").upper()
        coa_template_key = form.get("coa_template_key", "").strip() or "ee/default"

        errors: dict[str, str] = {}
        if not registrikood:
            errors["registrikood"] = _("Registrikood is required for an Estonian company.")
        elif not _REGISTRIKOOD_RE.match(registrikood):
            errors["registrikood"] = _("Registrikood must be exactly 8 digits.")
        if kmv_raw and not _KMV_RE.match(kmv_raw):
            errors["kmv"] = _("KMV/VAT number must be \"EE\" followed by 9 digits, e.g. EE123456789.")

        if errors:
            return _TEMPLATES.TemplateResponse(
                request,
                "companies/new.html",
                {"form": form, "errors": errors},
                status_code=422,
            )

        # Sent for real — see the module-level note above on why these
        # keys are included even though the engine does not accept them
        # yet (verified against saebooks/api/v1/schemas.py::CompanyCreate).
        payload["jurisdiction"] = jurisdiction
        payload["registrikood"] = registrikood
        if kmv_raw:
            payload["kmv"] = kmv_raw
        payload["base_currency"] = base_currency
        payload["coa_template_key"] = coa_template_key

    async with api_client(request) as client:
        resp = await client.post("/api/v1/companies", json=payload)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        if jurisdiction == "EE":
            request.session["flash"] = _(
                "Company created. Estonian jurisdiction, registrikood and "
                "KMV/VAT number were captured but could not be saved yet "
                "— the engine does not accept these fields on company "
                "creation. This is tracked separately; the values above "
                "were not persisted."
            )
        else:
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

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/companies/{company_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def company_hard_delete(request: Request, company_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/companies",
        entity_id=company_id,
        entity_label=f"Company {company_id}",
        list_url="/companies",
        detail_url=f"/companies/{company_id}",
    )
