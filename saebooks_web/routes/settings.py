"""Settings area — a coherent hub over every company/tenant setting.

Routes
------
GET  /settings                     — hub: grouped cards linking every setting
GET  /settings/company             — organisation / financial / policy form
POST /settings/company             — PATCH the active company with If-Match
GET  /settings/company/backdate-gst-confirm  — (kept) backdate confirm step
GET  /settings/api-tokens          — list + issue personal API tokens
POST /settings/api-tokens          — create a token (shown once)
POST /settings/api-tokens/{id}/revoke — revoke a token
GET  /settings/users               — team members (read-only; degrades if 404)
GET  /settings/preferences         — locale / theme / bookkeeping-mode surface

The web tier holds no business rules — it renders what the engine returns and
posts back what the user submits. Validation (field formats, fin-year day/month
cross-checks, optimistic-lock versions) is the engine's; this layer only surfaces
the 422/409 responses readably.
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
# Company field maps
# ---------------------------------------------------------------------------

# Plain free-text fields, always safe to clear (send stripped-or-None). ``name``
# is required (min_length=1) so it is handled separately — never blanked.
_TEXT_FIELDS = (
    "legal_name",
    "trading_name",
    "abn",
    "acn",
    "phone",
    "email",
    "website",
    "default_payment_terms",
    "payment_terms_text",
    "terms_url",
    "bank_name",
    "bank_bsb",
    "bank_account_number",
    "bank_account_name",
)
_ADDR_KEYS = ("line1", "line2", "city", "state", "postcode", "country")

# Enum passthroughs — recognised values only; engine does authoritative check.
_PSI_VALUES = ("yes", "no", "unsure")
_WRITEOFF_MODES = ("review", "auto", "manual")
_RECOVERY_MODES = ("smart_prompt", "manual", "reopen")
_COSTING_METHODS = ("weighted_average", "fifo", "quantity_only")
_LIFECYCLE_VALUES = ("active", "dormant", "in_liquidation", "deregistered")

# Full-accounting-only fields — hidden and NOT submitted in cashbook mode so a
# single-entry user never sees (or accidentally sets) accrual-only policy.
_FULL_ONLY_ENUM = {
    "psi_status": _PSI_VALUES,
    "writeoff_mode": _WRITEOFF_MODES,
    "recovery_mode": _RECOVERY_MODES,
    "costing_method": _COSTING_METHODS,
}
_FULL_ONLY_CLEARABLE_TEXT = (
    "bad_debt_recovery_account",
    "ar_control_account_code",
    "ap_control_account_code",
    "asset_disposal_gain_account_code",
    "asset_disposal_loss_account_code",
)


def _build_address(form: dict[str, str]) -> dict[str, str | None] | None:
    """Extract address sub-fields from the flat form dict.

    Returns a dict (possibly all-None) if any address key was submitted,
    or None if none were present at all.
    """
    addr: dict[str, str | None] = {}
    for key in _ADDR_KEYS:
        val = form.get(f"address_{key}", "").strip() or None
        addr[key] = val
    if any(v is not None for v in addr.values()):
        return addr
    return None


def _company_mode(company: dict | None) -> str:
    """Bookkeeping mode of the company record ("full" | "cashbook")."""
    return (company or {}).get("bookkeeping_mode") or "full"


def _prefill_form(company: dict) -> dict[str, str]:
    """Flatten a company record into a form dict for pre-filling the template."""
    form: dict[str, str] = {}
    form["name"] = str(company.get("name") or "")
    for field in _TEXT_FIELDS:
        form[field] = str(company.get(field) or "")

    form["version"] = str(company.get("version", ""))
    form["base_currency"] = str(company.get("base_currency") or "AUD")

    addr = company.get("address") or {}
    for key in _ADDR_KEYS:
        form[f"address_{key}"] = str(addr.get(key) or "")

    # tax_registered is the engine field (the checkbox was historically — and
    # wrongly — bound to a non-existent ``gst_registered`` key, so registration
    # state never round-tripped). gst_effective_date names the AU workflow.
    form["tax_registered"] = "true" if company.get("tax_registered") else ""
    form["gst_effective_date"] = str(company.get("gst_effective_date") or "")

    form["fin_year_start_month"] = str(company.get("fin_year_start_month") or 7)
    # Day-of-month precision unlocks only once the engine payload carries the
    # field; detected on the record rather than a hardcoded flag.
    if company.get("fin_year_start_day") is not None:
        form["fin_year_start_day"] = str(company.get("fin_year_start_day"))

    form["lifecycle_status"] = str(company.get("lifecycle_status") or "active")
    form["psi_status"] = str(company.get("psi_status") or "unsure")

    form["writeoff_mode"] = str(company.get("writeoff_mode") or "review")
    form["writeoff_threshold_days"] = str(company.get("writeoff_threshold_days") or 90)
    form["recovery_mode"] = str(company.get("recovery_mode") or "smart_prompt")
    form["costing_method"] = str(company.get("costing_method") or "weighted_average")
    for field in _FULL_ONLY_CLEARABLE_TEXT:
        form[field] = str(company.get(field) or "")

    # EE company-registry identifiers (read-through; NULL for AU companies).
    form["registrikood"] = str(company.get("registrikood") or "")
    form["kmv_number"] = str(company.get("kmv_number") or "")
    return form


def _company_context(
    request: Request,
    *,
    company: dict | None,
    form: dict[str, str],
    errors: dict[str, str],
    conflict: bool,
    flash: str | None,
    error: str | None,
) -> dict:
    """Shared template context for settings/company.html."""
    return {
        "company": company,
        "form": form,
        "errors": errors,
        "conflict": conflict,
        "flash": flash,
        "error": error,
        "mode": _company_mode(company),
        "jurisdiction": getattr(request.state, "active_company_jurisdiction", None) or "AU",
    }


# ---------------------------------------------------------------------------
# GET /settings — hub
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_hub(request: Request) -> HTMLResponse | RedirectResponse:
    """Grouped landing page linking every settings surface."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    mode = getattr(request.state, "active_company_bookkeeping_mode", "full") or "full"
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "settings/index.html",
        {
            "mode": mode,
            "jurisdiction": getattr(request.state, "active_company_jurisdiction", None) or "AU",
            "user_role": request.session.get("user_role", ""),
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# GET /settings/company
# ---------------------------------------------------------------------------


@router.get("/settings/company", response_class=HTMLResponse, response_model=None)
async def company_settings(
    request: Request,
    company_id: str | None = Query(default=None),
) -> HTMLResponse | RedirectResponse:
    """Render the company settings form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    company: dict | None = None
    error: str | None = None

    async with api_client(request) as client:
        if company_id:
            resp = await client.get(f"/api/v1/companies/{company_id}")
        else:
            resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})

    # We intentionally do NOT clear the session on a 401 here — other pages
    # tolerate a transient 401 by rendering empty; only this route used to
    # bounce the user out, which felt broken.
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

    form = _prefill_form(company) if company else {}
    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "settings/company.html",
        _company_context(
            request,
            company=company,
            form=form,
            errors={},
            conflict=False,
            flash=flash,
            error=error,
        ),
    )


# ---------------------------------------------------------------------------
# POST /settings/company
# ---------------------------------------------------------------------------


def _build_company_payload(form: dict[str, str], form_data, *, mode: str, jurisdiction: str) -> dict[str, object]:
    """Assemble the PATCH payload from the submitted form.

    Only recognised fields are forwarded; unknown/blank enum values are dropped
    and let the engine own validation. Full-accounting-only policy fields are
    skipped entirely in cashbook mode.
    """
    payload: dict[str, object] = {}

    # Required text — only send when non-empty so we never blank the name.
    name = form.get("name", "").strip()
    if name:
        payload["name"] = name

    # Optional text — present-but-empty clears (send None).
    for field in _TEXT_FIELDS:
        if field in form:
            payload[field] = form.get(field, "").strip() or None

    addr = _build_address(form)
    if addr is not None:
        payload["address"] = addr

    # GST/tax registration (checkbox present only when checked).
    payload["tax_registered"] = "tax_registered" in form_data
    gst_date = form.get("gst_effective_date", "").strip()
    if gst_date:
        payload["gst_effective_date"] = gst_date

    # Financial year — month always; day only once the engine models it (the
    # disabled placeholder input carries no name, so it is absent until then).
    fy_month_raw = form.get("fin_year_start_month", "").strip()
    if fy_month_raw:
        payload["fin_year_start_month"] = _as_int(fy_month_raw)
    if "fin_year_start_day" in form:
        day_raw = form.get("fin_year_start_day", "").strip()
        if day_raw:
            payload["fin_year_start_day"] = _as_int(day_raw)

    # Entity lifecycle.
    lifecycle = form.get("lifecycle_status", "").strip()
    if lifecycle in _LIFECYCLE_VALUES:
        payload["lifecycle_status"] = lifecycle

    # EE company-registry identifiers — only meaningful on EE companies.
    if jurisdiction == "EE":
        for field in ("registrikood", "kmv_number"):
            if field in form:
                payload[field] = form.get(field, "").strip() or None

    # base_currency is deliberately read-only on this form: changing it on a
    # company with posted transactions is not a label edit. Never submitted.

    if mode != "full":
        return payload

    # --- full-accounting-only policy fields ---
    for field, allowed in _FULL_ONLY_ENUM.items():
        val = form.get(field, "").strip()
        if val in allowed:
            payload[field] = val
    threshold_raw = form.get("writeoff_threshold_days", "").strip()
    if threshold_raw:
        payload["writeoff_threshold_days"] = _as_int(threshold_raw)
    for field in _FULL_ONLY_CLEARABLE_TEXT:
        if field in form:
            payload[field] = form.get(field, "").strip() or None
    return payload


def _as_int(raw: str) -> object:
    """Int-or-passthrough: forward a non-numeric string so the engine 422s
    (and the per-field error renders) rather than swallowing it."""
    try:
        return int(raw)
    except ValueError:
        return raw


@router.post("/settings/company", response_class=HTMLResponse, response_model=None)
async def company_settings_update(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the company settings form — PATCH with If-Match."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    company_id: str | None = form.get("company_id") or None
    version = form.get("version", "")

    # Re-resolve company_id when it wasn't on the page (no company at load).
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
            _company_context(
                request,
                company=None,
                form=form,
                errors={"__all__": "No company record found — contact your administrator."},
                conflict=False,
                flash=None,
                error=None,
            ),
        )

    mode = form.get("bookkeeping_mode") or "full"
    jurisdiction = getattr(request.state, "active_company_jurisdiction", None) or "AU"
    payload = _build_company_payload(form, form_data, mode=mode, jurisdiction=jurisdiction)

    gst_date = form.get("gst_effective_date", "").strip()
    backdate_confirmed = form.get("backdate_confirmed", "") == "true"

    # Backdate confirmation: only block the save when the preview reports
    # pre-registration invoices that would need credit notes.
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
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "settings/company.html",
            _company_context(
                request,
                company=server_company,
                form=conflict_form,
                errors={},
                conflict=True,
                flash=None,
                error=None,
            ),
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

    # Re-fetch the company for a valid object to re-render against.
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
        _company_context(
            request,
            company=company,
            form=form,
            errors=errors,
            conflict=False,
            flash=None,
            error=None,
        ),
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# GET/POST /settings/api-tokens
# ---------------------------------------------------------------------------


@router.get("/admin/api-tokens", response_model=None, include_in_schema=False)
async def api_tokens_legacy_redirect() -> RedirectResponse:
    """MCP/connector docs historically said tokens are issued at
    /admin/api-tokens; keep that path working."""
    return RedirectResponse(url="/settings/api-tokens", status_code=308)


@router.get("/settings/api-tokens", response_class=HTMLResponse, response_model=None)
async def api_tokens_page(request: Request) -> HTMLResponse | RedirectResponse:
    """List personal API tokens and offer to issue a new one."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    tokens: list[dict] = []
    error: str | None = None
    degraded = False
    async with api_client(request) as client:
        resp = await client.get("/api/v1/api-tokens")
    if resp.is_success:
        payload = resp.json()
        tokens = payload if isinstance(payload, list) else payload.get("items", [])
    elif resp.status_code == 404:
        degraded = True
    elif resp.status_code == 401:
        error = "Your session may have expired — please sign in again."
    else:
        error = f"API error: HTTP {resp.status_code}"

    # A freshly-issued token cleartext is stashed once in the session by the
    # POST handler (it is never retrievable again) and consumed here.
    new_token = request.session.pop("new_api_token", None)
    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "settings/api_tokens.html",
        {
            "tokens": tokens,
            "new_token": new_token,
            "flash": flash,
            "error": error,
            "degraded": degraded,
            "errors": {},
            "mode": getattr(request.state, "active_company_bookkeeping_mode", "full") or "full",
        },
    )


@router.post("/settings/api-tokens", response_class=HTMLResponse, response_model=None)
async def api_tokens_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Issue a new API token; stash the one-time cleartext in the session."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    name = str(form_data.get("name", "")).strip()
    ttl_raw = str(form_data.get("ttl_days", "")).strip()

    if not name:
        return _TEMPLATES.TemplateResponse(
            request,
            "settings/api_tokens.html",
            {
                "tokens": await _list_tokens(request),
                "new_token": None,
                "flash": None,
                "error": None,
                "degraded": False,
                "errors": {"name": "Give the token a name so you can recognise it later."},
                "mode": getattr(request.state, "active_company_bookkeeping_mode", "full") or "full",
            },
            status_code=422,
        )

    body: dict[str, object] = {"name": name}
    if ttl_raw:
        try:
            body["ttl_days"] = int(ttl_raw)
        except ValueError:
            pass

    async with api_client(request) as client:
        resp = await client.post("/api/v1/api-tokens", json=body)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (200, 201):
        data = resp.json()
        # Cleartext is shown ONCE. It is the user's own credential rendered in
        # their own browser — never logged or echoed elsewhere.
        request.session["new_api_token"] = {
            "name": data.get("name", name),
            "token": data.get("token", ""),
            "prefix": data.get("token_prefix", ""),
        }
        request.session["flash"] = "API token created."
        return RedirectResponse(url="/settings/api-tokens", status_code=303)

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors["name"] = "The engine rejected that token name."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"
    return _TEMPLATES.TemplateResponse(
        request,
        "settings/api_tokens.html",
        {
            "tokens": await _list_tokens(request),
            "new_token": None,
            "flash": None,
            "error": None,
            "degraded": False,
            "errors": errors,
            "mode": getattr(request.state, "active_company_bookkeeping_mode", "full") or "full",
        },
        status_code=resp.status_code if resp.status_code >= 400 else 422,
    )


@router.post("/settings/api-tokens/{token_id}/revoke", response_class=HTMLResponse, response_model=None)
async def api_tokens_revoke(request: Request, token_id: str) -> RedirectResponse:
    """Revoke a token by id."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/api-tokens/{token_id}")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    request.session["flash"] = (
        "API token revoked." if resp.is_success else f"Could not revoke token (HTTP {resp.status_code})."
    )
    return RedirectResponse(url="/settings/api-tokens", status_code=303)


async def _list_tokens(request: Request) -> list[dict]:
    async with api_client(request) as client:
        resp = await client.get("/api/v1/api-tokens")
    if resp.is_success:
        payload = resp.json()
        return payload if isinstance(payload, list) else payload.get("items", [])
    return []


# ---------------------------------------------------------------------------
# GET /settings/users
# ---------------------------------------------------------------------------


@router.get("/settings/users", response_class=HTMLResponse, response_model=None)
async def users_page(request: Request) -> HTMLResponse | RedirectResponse:
    """Read-only team roster. Degrades to an M2 banner when the module is
    unavailable on this edition (404) rather than showing a blank page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    users: list[dict] = []
    error: str | None = None
    degraded = False
    async with api_client(request) as client:
        resp = await client.get("/api/v1/users")
    if resp.is_success:
        payload = resp.json()
        users = payload if isinstance(payload, list) else payload.get("items", [])
    elif resp.status_code == 404:
        degraded = True
    elif resp.status_code in (401, 403):
        error = "You don't have permission to view team members, or your session expired."
    else:
        error = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "settings/users.html",
        {
            "users": users,
            "error": error,
            "degraded": degraded,
            "current_email": request.session.get("username", ""),
            "mode": getattr(request.state, "active_company_bookkeeping_mode", "full") or "full",
        },
    )


# ---------------------------------------------------------------------------
# GET /settings/preferences
# ---------------------------------------------------------------------------


@router.get("/settings/preferences", response_class=HTMLResponse, response_model=None)
async def preferences_page(request: Request) -> HTMLResponse | RedirectResponse:
    """Locale, theme and bookkeeping-mode surface (read-through)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "settings/preferences.html",
        {
            "mode": getattr(request.state, "active_company_bookkeeping_mode", "full") or "full",
            "jurisdiction": getattr(request.state, "active_company_jurisdiction", None) or "AU",
            "active_locale": getattr(request.state, "locale", None) or request.session.get("locale", "en"),
            "flash": flash,
        },
    )
