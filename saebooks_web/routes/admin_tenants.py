"""Web-layer tenant switcher.

  GET  /admin/tenants                — pick page (lists all tenants)
  POST /admin/tenants/switch         — sets ``active_tenant_override`` on the
                                       session; subsequent api_client calls
                                       include the X-Active-Tenant header.
  POST /admin/tenants/reset          — clears the override.

Gated by ``is_feature_enabled("tenant_switcher")``. Hidden / 404 otherwise.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.features import is_feature_enabled

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


@router.get("/admin/tenants", response_class=HTMLResponse, response_model=None)
async def tenants_page(request: Request):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not is_feature_enabled("tenant_switcher"):
        return HTMLResponse("Not found", status_code=404)

    async with api_client(request) as client:
        resp = await client.get("/api/v1/admin/tenants")
    tenants = resp.json().get("items", []) if resp.is_success else []
    active = request.session.get("active_tenant_override") or ""

    return _TEMPLATES.TemplateResponse(
        request,
        "admin/tenants.html",
        {
            "tenants": tenants,
            "active_override": active,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/admin/tenants/switch", response_class=HTMLResponse, response_model=None)
async def tenants_switch(request: Request, tenant_id: str = Form(...)):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not is_feature_enabled("tenant_switcher"):
        return HTMLResponse("Not found", status_code=404)
    request.session["active_tenant_override"] = tenant_id.strip()
    request.session["flash"] = f"Active tenant: {tenant_id[:8]}…"
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.post("/admin/tenants/reset", response_class=HTMLResponse, response_model=None)
async def tenants_reset(request: Request):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    request.session.pop("active_tenant_override", None)
    request.session["flash"] = "Tenant override cleared."
    return RedirectResponse(url="/admin/tenants", status_code=303)
