"""Company settings view — Lane D.

GET  /settings/company  — load first company from API, render form
POST /settings/company  — PATCH first company with If-Match; PRG on success

The company entity has an ``address`` JSONB field sent as a nested dict.
Address sub-fields are submitted as ``address_line1``, ``address_city``, etc.
and assembled into ``{"line1": ..., "city": ..., ...}`` (empty strings stripped).
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
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOP_FIELDS = ("name", "legal_name", "trading_name", "abn")
_ADDR_KEYS = ("line1", "line2", "city", "state", "postcode", "country")


def _build_address(form: dict[str, str]) -> dict[str, str | None] | None:
    """Extract address sub-fields from the flat form dict.

    Returns a dict (possibly all-None) if any address key was submitted,
    or None if none were present at all.
    """
    addr: dict[str, str | None] = {}
    for key in _ADDR_KEYS:
        val = form.get(f"address_{key}", "").strip() or None
        addr[key] = val
    # Only include the address field in the payload when at least one key exists.
    if any(v is not None for v in addr.values()):
        return addr
    return None


# ---------------------------------------------------------------------------
# GET /settings/company
# ---------------------------------------------------------------------------


@router.get("/settings/company", response_class=HTMLResponse, response_model=None)
async def company_settings(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the company settings form.

    Fetches the first company from GET /api/v1/companies and pre-fills the
    form.  Shows an empty state when no company exists.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company: dict | None = None
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        items = resp.json().get("items", [])
        if items:
            company = items[0]
    else:
        error = f"API error: HTTP {resp.status_code}"

    # Build a flat form dict from the company record for pre-filling.
    form: dict[str, str] = {}
    if company:
        for field in _TOP_FIELDS:
            form[field] = str(company.get(field) or "")
        form["version"] = str(company.get("version", ""))
        addr = company.get("address") or {}
        for key in _ADDR_KEYS:
            form[f"address_{key}"] = str(addr.get(key) or "")

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "settings/company.html",
        {
            "company": company,
            "form": form,
            "errors": {},
            "conflict": False,
            "flash": flash,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# POST /settings/company
# ---------------------------------------------------------------------------


@router.post("/settings/company", response_class=HTMLResponse, response_model=None)
async def company_settings_update(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the company settings form — PATCH with If-Match.

    Outcomes:
    - 200 OK        -> flash "Company settings saved." + 303 redirect to self
    - 409 Conflict  -> re-render with conflict message + server's current version
    - 422           -> re-render with per-field validation errors
    - other errors  -> re-render with generic error message
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    company_id: str | None = form.get("company_id")
    version = form.get("version", "")

    # Build the PATCH payload.
    payload: dict[str, object] = {}
    for field in _TOP_FIELDS:
        val = form.get(field, "").strip() or None
        if val is not None:
            payload[field] = val

    addr = _build_address(form)
    if addr is not None:
        payload["address"] = addr

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/companies/{company_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Company settings saved."
        return RedirectResponse(url="/settings/company", status_code=303)

    # --- 409 Conflict --- re-fetch server's current state
    if resp.status_code == 409:
        server_company: dict = {}
        async with api_client(request) as client:
            latest = await client.get(f"/api/v1/companies/{company_id}")
        if latest.is_success:
            server_company = latest.json()

        server_version = str(server_company.get("version", ""))
        # Keep user's submitted values but bump version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "settings/company.html",
            {
                "company": server_company,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "flash": None,
                "error": None,
            },
            status_code=409,
        )

    # --- 422 Validation errors ---
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field = str(field_parts[0]) if field_parts else "__all__"
                    errors[field] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch the company to have a valid object for the template.
    company: dict | None = None
    async with api_client(request) as client:
        clist = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
    if clist.is_success:
        items = clist.json().get("items", [])
        if items:
            company = items[0]

    return _TEMPLATES.TemplateResponse(
        request,
        "settings/company.html",
        {
            "company": company,
            "form": form,
            "errors": errors,
            "conflict": False,
            "flash": None,
            "error": None,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )
