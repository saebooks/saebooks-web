"""Bank rules list, create, edit, delete, and apply-all — Lane D cycle 47.

GET  /bank-rules                — list all bank rules
GET  /bank-rules/new            — create form
POST /bank-rules/new            — submit -> redirect to list on 201
GET  /bank-rules/{id}/edit      — edit form pre-filled
POST /bank-rules/{id}/edit      — submit PATCH -> redirect on 200, conflict banner on 409
POST /bank-rules/{id}/delete    — DELETE -> redirect to list
POST /bank-rules/apply-all      — trigger bulk apply -> redirect with flash

Route ordering: /new + /apply-all MUST appear before /{rule_id} paths so
FastAPI matches literal paths first.

API calls:
- GET   /api/v1/bank_rules
- POST  /api/v1/bank_rules
- GET   /api/v1/bank_rules/{id}
- PATCH /api/v1/bank_rules/{id} with If-Match
- DELETE /api/v1/bank_rules/{id} with If-Match
- POST  /api/v1/bank_rules/apply

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Engine fields (api/v1/schemas.BankRuleCreate). Batch 4 (P1-18): this form
# used to post a made-up schema (match_field / match_operator / match_value /
# action_account_id) that the engine silently ignored and then rejected, so
# no rule could be created from the screens at all.
_EDIT_FIELDS = (
    "name",
    "match_type",
    "match_pattern",
    "account_id",
    "tax_code",
    "contact_id",
    "description_template",
    "priority",
    "direction",
    "amount_min",
    "amount_max",
    "bank_account_id",
)
_MATCH_TYPES = ("CONTAINS", "STARTS_WITH", "EXACT", "REGEX")


def _payload_from_form(form: dict[str, str], *, partial: bool = False) -> dict[str, object]:
    """Build the engine payload from the submitted form."""
    payload: dict[str, object] = {}
    for field in ("name", "match_pattern", "account_id"):
        val = (form.get(field) or "").strip()
        if val:
            payload[field] = val
    mt = (form.get("match_type") or "CONTAINS").strip().upper()
    payload["match_type"] = mt if mt in _MATCH_TYPES else "CONTAINS"
    for field in ("tax_code", "contact_id", "description_template", "bank_account_id"):
        val = (form.get(field) or "").strip()
        payload[field] = val or None
    direction = (form.get("direction") or "ANY").strip().upper()
    payload["direction"] = direction if direction in ("ANY", "IN", "OUT") else "ANY"
    for field in ("amount_min", "amount_max"):
        val = (form.get(field) or "").strip().replace(",", "").replace("$", "")
        payload[field] = val or None
    priority_raw = (form.get("priority") or "").strip()
    if priority_raw:
        try:
            payload["priority"] = int(priority_raw)
        except ValueError:
            payload["priority"] = priority_raw
    payload["auto_create"] = (form.get("auto_create") or "").lower() in ("1", "on", "true")
    payload["is_active"] = (form.get("is_active", "on") or "").lower() in ("1", "on", "true")
    return payload


async def _form_options(client) -> dict[str, list[dict]]:
    """Accounts, GST codes, contacts and bank accounts for the rule form."""
    accounts: list[dict] = []
    for t in ("EXPENSE", "COST_OF_SALES", "OTHER_EXPENSE", "INCOME", "OTHER_INCOME",
              "ASSET", "LIABILITY", "EQUITY"):
        try:
            r = await client.get("/api/v1/accounts", params={"account_type": t, "limit": 500, "offset": 0})
        except Exception:  # noqa: BLE001 — options are best-effort
            continue
        if r.is_success:
            accounts.extend(a for a in r.json().get("items", []) if not a.get("is_header"))
    accounts.sort(key=lambda a: a.get("code", ""))
    tax_codes: list[dict] = []
    contacts: list[dict] = []
    banks: list[dict] = []
    try:
        r = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if r.is_success:
            tax_codes = r.json().get("items", [])
        r = await client.get("/api/v1/contacts", params={"limit": 500, "offset": 0})
        if r.is_success:
            contacts = sorted(r.json().get("items", []), key=lambda c: (c.get("name") or "").lower())
        r = await client.get("/api/v1/reconciliation/accounts")
        if r.is_success:
            payload = r.json()
            banks = payload if isinstance(payload, list) else payload.get("items", [])
    except Exception:  # noqa: BLE001
        pass
    return {"accounts": accounts, "tax_codes": tax_codes, "contacts": contacts, "banks": banks}


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _parse_errors(resp_json: dict) -> dict[str, str]:
    """Parse a 422 response body into a field->message error dict."""
    errors: dict[str, str] = {}
    try:
        detail = resp_json.get("detail", [])
        if isinstance(detail, list):
            for err in detail:
                loc = err.get("loc", [])
                field_parts = [p for p in loc if p != "body"]
                field = str(field_parts[0]) if field_parts else "__all__"
                errors[field] = err.get("msg", "Invalid value")
        elif isinstance(detail, str):
            errors["__all__"] = detail
    except Exception:
        errors["__all__"] = "Validation error"
    return errors


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/bank-rules", response_class=HTMLResponse, response_model=None)
async def bank_rules_list(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the bank rules list page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    rules: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank_rules")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            rules = payload if isinstance(payload, list) else payload.get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

        account_names: dict[str, str] = {}
        if rules:
            try:
                options = await _form_options(client)
                account_names = {
                    a["id"]: f"{a.get('code', '')} {a.get('name', '')}".strip()
                    for a in options["accounts"] + options["banks"]
                }
                account_names.update({c["id"]: c.get("name", "") for c in options["contacts"]})
            except Exception:  # noqa: BLE001 — names are a nicety
                account_names = {}

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/list.html",
        {
            "rules": rules,
            "names": account_names,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Create — GET (blank form) + POST (submit)
# NOTE: MUST appear before /{rule_id} paths.
# ---------------------------------------------------------------------------


@router.get("/bank-rules/new", response_class=HTMLResponse, response_model=None)
async def bank_rule_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the blank bank rule create form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # "Make a rule from this line" on the reconcile screen pre-fills these.
    q = request.query_params
    form: dict[str, str] = {}
    if q.get("pattern"):
        pattern = q.get("pattern", "").strip()[:120]
        form["match_pattern"] = pattern
        form["name"] = pattern[:60]
    for key in ("direction", "bank_account_id", "account_id"):
        if q.get(key):
            form[key] = q.get(key, "")

    async with api_client(request) as client:
        options = await _form_options(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/new.html",
        {
            "form": form,
            "errors": {},
            **options,
        },
    )


@router.post("/bank-rules/new", response_class=HTMLResponse, response_model=None)
async def bank_rule_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-bank-rule form.

    - 201 -> 303 redirect to /bank-rules
    - 422 -> re-render form with per-field errors
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]
    payload = _payload_from_form(form)

    async with api_client(request) as client:
        resp = await client.post("/api/v1/bank_rules", json=payload)

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.status_code == 201:
            request.session["flash"] = _("Bank rule created. It runs on every statement you import.")
            return RedirectResponse(url="/bank-rules", status_code=303)

        options = await _form_options(client)

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_errors(resp.json())
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/new.html",
        {"form": form, "errors": errors, **options},
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Apply all — POST /bank-rules/apply-all
# NOTE: MUST appear before /{rule_id} paths.
# ---------------------------------------------------------------------------


@router.post("/bank-rules/apply-all", response_class=HTMLResponse, response_model=None)
async def bank_rules_apply_all(request: Request) -> RedirectResponse:
    """Trigger bulk apply of all bank rules.

    POSTs to POST /api/v1/bank_rules/apply and redirects to the list
    with a flash message showing how many lines were categorised.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post("/api/v1/bank_rules/apply")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        try:
            data = resp.json()
            applied = data.get("applied", data.get("count", ""))
            if applied != "":
                request.session["flash"] = f"Bank rules applied — {applied} line(s) categorised."
            else:
                request.session["flash"] = "Bank rules applied."
        except Exception:
            request.session["flash"] = "Bank rules applied."
    else:
        request.session["flash"] = f"Apply failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/bank-rules", status_code=303)


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear after /new and /apply-all.
# ---------------------------------------------------------------------------


@router.get(
    "/bank-rules/{rule_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_rule_edit_form(
    request: Request,
    rule_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing bank rule."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_rules/{rule_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_rules/edit.html",
            {
                "rule": None,
                "form": {},
                "errors": {"__all__": "Bank rule not found"},
                "conflict": False,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_rules/edit.html",
            {
                "rule": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
            },
            status_code=resp.status_code,
        )

    rule_obj = resp.json()
    form: dict[str, str] = {field: str(rule_obj.get(field) or "") for field in _EDIT_FIELDS}
    form["auto_create"] = "on" if rule_obj.get("auto_create") else ""
    form["is_active"] = "on" if rule_obj.get("is_active", True) else ""

    async with api_client(request) as client:
        options = await _form_options(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/edit.html",
        {
            "rule": rule_obj,
            "form": form,
            "errors": {},
            "conflict": False,
            **options,
        },
    )


@router.post(
    "/bank-rules/{rule_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_rule_update(
    request: Request,
    rule_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /bank-rules
    - 409 Conflict -> re-fetch latest, re-render with conflict banner
    - 422          -> re-render with per-field validation errors
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]
    payload = _payload_from_form(form)

    async with api_client(request) as client:
        # BankRule has no version column — the engine PATCH takes no If-Match.
        resp = await client.patch(f"/api/v1/bank_rules/{rule_id}", json=payload)

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.status_code == 200:
            request.session["flash"] = _("Bank rule saved.")
            return RedirectResponse(url="/bank-rules", status_code=303)

        options = await _form_options(client)

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        errors = _parse_errors(resp.json())
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_rules/edit.html",
        {
            "rule": {"id": rule_id},
            "form": form,
            "errors": errors,
            "conflict": False,
            **options,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Delete — POST /{rule_id}/delete
# ---------------------------------------------------------------------------


@router.post(
    "/bank-rules/{rule_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_rule_delete(
    request: Request,
    rule_id: str,
) -> RedirectResponse:
    """Delete a bank rule via DELETE /api/v1/bank_rules/{id} with If-Match."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))

    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/bank_rules/{rule_id}",
            headers={"If-Match": version} if version else {},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success or resp.status_code == 204:
        request.session["flash"] = "Bank rule deleted."
    elif resp.status_code == 409:
        request.session["flash"] = "Delete failed: version conflict. Please retry."
    else:
        request.session["flash"] = f"Delete failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/bank-rules", status_code=303)


# ---------------------------------------------------------------------------
# Bulk action — POST /bank-rules/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_BANK_RULES = {
    "delete": ("DELETE", "/api/v1/bank_rules/{id}"),
}


@router.post("/bank-rules/bulk", response_class=HTMLResponse, response_model=None)
async def bank_rules_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many bank rules at once.

    Form fields:
      action  — one of: delete
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /bank-rules. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_BANK_RULES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/bank-rules", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/bank-rules", status_code=303)

    method, path_tpl = _BULK_ACTIONS_BANK_RULES[action]
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
        request.session["flash"] = f"{label}: {ok} bank rule{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/bank-rules", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/bank-rules/{rule_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def bank_rule_hard_delete(request: Request, rule_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/bank_rules",
        entity_id=rule_id,
        entity_label=f"Bank rule {rule_id}",
        list_url="/bank-rules",
        detail_url=f"/bank-rules/{rule_id}",
    )
