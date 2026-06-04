"""Extraction-template management views — Gitea #28, Phase 4.

Route map
---------
GET  /statement-templates            — list all templates
POST /statement-templates/{id}/delete — delete a template (→ api DELETE) + redirect

Auth guard: redirect to /login (303) if no session token.

API endpoints consumed:
- GET    /api/v1/statement-templates            params: contact_id=, supplier_abn=
- DELETE /api/v1/statement-templates/{id}       → 204
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
    """Return the token if present, else None."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# GET /statement-templates — list all templates
# ---------------------------------------------------------------------------


@router.get("/statement-templates", response_class=HTMLResponse, response_model=None)
async def statement_templates_list(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Render the extraction-template management page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    templates: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/statement-templates")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            templates = payload.get("items", payload) if isinstance(payload, dict) else payload
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "statements/templates.html",
        {
            "templates": templates,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /statement-templates/{id}/delete — delete a template
# ---------------------------------------------------------------------------


@router.post(
    "/statement-templates/{template_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def statement_templates_delete(
    request: Request,
    template_id: str,
) -> RedirectResponse:
    """Delete an extraction template via the API, then redirect to the list."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/statement-templates/{template_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 204 or resp.is_success:
        request.session["flash"] = "Template deleted."
    else:
        try:
            msg = resp.json().get("detail", f"Delete failed: HTTP {resp.status_code}")
        except Exception:  # noqa: BLE001
            msg = f"Delete failed: HTTP {resp.status_code}"
        request.session["flash"] = str(msg)

    return RedirectResponse(url="/statement-templates", status_code=303)
