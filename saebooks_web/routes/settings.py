"""Company settings view — Lane D.

GET  /settings/company  — load first company from API, render form
POST /settings/company  — PATCH first company with If-Match; PRG on success
GET  /settings/company/backdate-gst-confirm  — confirmation step for backdated GST date

The company entity has an ``address`` JSONB field sent as a nested dict.
Address sub-fields are submitted as ``address_line1``, ``address_city``, etc.
and assembled into ``{"line1": ..., "city": ..., ...}`` (empty strings stripped).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

# Dates this far in the past trigger the retroactive-recompute confirmation step.
_BACKDATE_CONFIRM_DAYS = 21

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
_GST_FIELDS = ("gst_registered", "gst_effective_date")


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
async def company_settings(
    request: Request,
    company_id: str | None = Query(default=None),
) -> HTMLResponse | RedirectResponse:
    """Render the company settings form.

    If ``company_id`` query param is provided, fetches that specific company.
    Otherwise falls back to the first company in the list.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company: dict | None = None
    error: str | None = None

    async with api_client(request) as client:
        if company_id:
            resp = await client.get(f"/api/v1/companies/{company_id}")
        else:
            resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})

    # We intentionally do NOT clear the session and redirect to /login on a
    # 401 from the upstream API. Most other pages (dashboard, list views)
    # tolerate a transient 401 by rendering with empty data; only this
    # route used to bounce the user out, which felt broken because every
    # other link in the nav worked. If the upstream actually rejects the
    # token, the user can use the navbar Sign-In control — we don't punt
    # them out from a deep link.
    if resp.is_success:
        if company_id:
            company = resp.json()
        else:
            items = resp.json().get("items", [])
            if items:
                company = items[0]
    elif resp.status_code == 401:
        error = "Your session may have expired — please sign in again."
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
        form["gst_registered"] = "true" if company.get("gst_registered") else ""
        form["gst_effective_date"] = str(company.get("gst_effective_date") or "")
        form["psi_status"] = str(company.get("psi_status") or "unsure")

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

    company_id: str | None = form.get("company_id") or None
    version = form.get("version", "")

    # Guard: company_id is missing when no company existed at page-load time.
    # Re-resolve from the API before proceeding so we don't build a broken URL.
    if not company_id:
        async with api_client(request) as client:
            _clist = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if _clist.is_success:
            _items = _clist.json().get("items", [])
            if _items:
                company_id = str(_items[0]["id"])
    if not company_id:
        return _TEMPLATES.TemplateResponse(
            request,
            "settings/company.html",
            {
                "company": None,
                "form": form,
                "errors": {"__all__": "No company record found — contact your administrator."},
                "conflict": False,
                "flash": None,
                "error": None,
            },
        )

    # Build the PATCH payload.
    payload: dict[str, object] = {}
    for field in _TOP_FIELDS:
        val = form.get(field, "").strip() or None
        if val is not None:
            payload[field] = val

    addr = _build_address(form)
    if addr is not None:
        payload["address"] = addr

    # gst_registered is a checkbox — present in form data only when checked.
    payload["gst_registered"] = "gst_registered" in form_data
    psi_status = form.get("psi_status", "").strip()
    if psi_status in ("yes", "no", "unsure"):
        payload["psi_status"] = psi_status
    gst_date = form.get("gst_effective_date", "").strip()
    backdate_confirmed = form.get("backdate_confirmed", "") == "true"
    if gst_date:
        payload["gst_effective_date"] = gst_date

    # If the date is significantly in the past AND there are pre-registration
    # invoices that would need credit notes, show the backdate-confirm page.
    #
    # Past behaviour fired the confirm for any date >21 days ago. That mis-
    # handles the common scenario where a long-standing GST-registered
    # business is just recording its historical effective date in the books
    # — no backdating event is occurring, the ATO already has the
    # registration on file, and there are no pre-registration invoices to
    # re-issue. We only block the save when the preview reports
    # ``invoice_count > 0``; otherwise the save proceeds silently.
    if gst_date and not backdate_confirmed:
        try:
            eff_date = date.fromisoformat(gst_date)
            cutoff = date.today() - timedelta(days=_BACKDATE_CONFIRM_DAYS)
            if eff_date <= cutoff:
                invoice_count = 0
                async with api_client(request) as client:
                    preview = await client.get(
                        f"/api/v1/companies/{company_id}/gst-backdate-preview",
                        params={"effective_date": gst_date},
                    )
                if preview.is_success:
                    invoice_count = preview.json().get("invoice_count", 0)
                if invoice_count > 0:
                    return _TEMPLATES.TemplateResponse(
                        request,
                        "settings/gst_backdate_confirm.html",
                        {
                            "company_id": company_id,
                            "version": version,
                            "gst_date": gst_date,
                            "invoice_count": invoice_count,
                            "form": form,
                        },
                    )
                # else: no pre-registration invoices — fall through and save
        except ValueError:
            pass  # unparseable date — let the API return 422

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
        if gst_date and backdate_confirmed:
            request.session["flash"] = (
                "GST registration backdated. Review invoices issued from "
                f"{gst_date} onward and issue credit notes where GST was not charged."
            )
        else:
            request.session["flash"] = "Company settings saved."
        return RedirectResponse(url=f"/settings/company?company_id={company_id}", status_code=303)

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
