"""Allocation rules views — overhead cost-pool distribution.

Regression: /allocations and /settings/allocations
previously returned 404. This module adds the web UI for the allocation
rules engine.

GET  /allocations                   — list all rules
GET  /settings/allocations          — redirect to /allocations (settings alias)
GET  /allocations/new               — create form
POST /allocations/new               — submit create
GET  /allocations/{id}              — rule detail + apply form
GET  /allocations/{id}/edit         — edit form
POST /allocations/{id}/edit         — submit update
POST /allocations/{id}/delete       — soft-archive
POST /allocations/{id}/apply        — apply rule (generate JE)

Route ordering: /new + /{id}/... MUST appear before the catch-all /{id}.
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


async def _fetch_accounts(request: Request) -> list[dict]:
    async with api_client(request) as client:
        resp = await client.get("/api/v1/accounts", params={"limit": 500, "offset": 0})
    if resp.is_success:
        return resp.json().get("items", [])
    return []


# ---------------------------------------------------------------------------
# Settings alias
# ---------------------------------------------------------------------------


@router.get("/settings/allocations", response_class=HTMLResponse, response_model=None)
async def settings_allocations_redirect(
    request: Request,
) -> RedirectResponse:
    return RedirectResponse(url="/allocations", status_code=302)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/allocations", response_class=HTMLResponse, response_model=None)
async def allocations_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    flash = request.session.pop("flash", None)
    items: list[dict] = []
    total = 0
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/allocation_rules",
            params={"page": 1, "page_size": 200},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 404:
        # Feature not enabled on this edition
        error = "Allocation rules require Business edition or higher."
    elif resp.is_success:
        data = resp.json()
        items = data.get("items", [])
        total = data.get("total", 0)
    else:
        error = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/list.html",
        {
            "rules": items,
            "total": total,
            "flash": flash,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Create — GET form
# ---------------------------------------------------------------------------


@router.get("/allocations/new", response_class=HTMLResponse, response_model=None)
async def allocation_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts = await _fetch_accounts(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/new.html",
        {
            "form": {},
            "errors": {},
            "accounts": accounts,
        },
    )


# ---------------------------------------------------------------------------
# Create — POST submit
# ---------------------------------------------------------------------------


@router.post("/allocations/new", response_class=HTMLResponse, response_model=None)
async def allocation_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    # Build targets list from repeated form fields:
    # target_account_id_0, target_label_0, target_percentage_0, ...
    targets: list[dict] = []
    i = 0
    while f"target_account_id_{i}" in form:
        acct_id = form.get(f"target_account_id_{i}", "").strip()
        label = form.get(f"target_label_{i}", "").strip()
        pct = form.get(f"target_percentage_{i}", "").strip()
        if acct_id and pct:
            targets.append({
                "account_id": acct_id,
                "label": label,
                "percentage": float(pct),
            })
        i += 1

    payload: dict = {
        "name": form.get("name", "").strip(),
        "description": form.get("description", "").strip() or None,
        "source_account_id": form.get("source_account_id", "").strip(),
        "targets": targets,
        "is_active": "is_active" in form_data,
    }

    async with api_client(request) as client:
        resp = await client.post("/api/v1/allocation_rules", json=payload)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        rule_id = resp.json().get("id", "")
        request.session["flash"] = f"Allocation rule '{payload['name']}' created."
        return RedirectResponse(url=f"/allocations/{rule_id}", status_code=303)

    # 404 → feature not enabled
    if resp.status_code == 404:
        accounts = await _fetch_accounts(request)
        return _TEMPLATES.TemplateResponse(
            request,
            "allocations/new.html",
            {
                "form": form,
                "errors": {"__all__": "Allocation rules require Business edition or higher."},
                "accounts": accounts,
            },
            status_code=422,
        )

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p not in ("body",)]
                    field = str(field_parts[0]) if field_parts else "__all__"
                    errors[field] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts = await _fetch_accounts(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/new.html",
        {"form": form, "errors": errors, "accounts": accounts},
        status_code=422,
    )


# ---------------------------------------------------------------------------
# Edit — GET form  (must appear before /{id} catch-all)
# ---------------------------------------------------------------------------


@router.get(
    "/allocations/{rule_id}/edit", response_class=HTMLResponse, response_model=None
)
async def allocation_edit_form(
    request: Request, rule_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/allocation_rules/{rule_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        return RedirectResponse(url="/allocations", status_code=303)

    rule = resp.json()
    accounts = await _fetch_accounts(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/edit.html",
        {"rule": rule, "form": rule, "errors": {}, "accounts": accounts},
    )


# ---------------------------------------------------------------------------
# Edit — POST submit
# ---------------------------------------------------------------------------


@router.post(
    "/allocations/{rule_id}/edit", response_class=HTMLResponse, response_model=None
)
async def allocation_update(
    request: Request, rule_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}

    targets: list[dict] = []
    i = 0
    while f"target_account_id_{i}" in form:
        acct_id = form.get(f"target_account_id_{i}", "").strip()
        label = form.get(f"target_label_{i}", "").strip()
        pct = form.get(f"target_percentage_{i}", "").strip()
        if acct_id and pct:
            targets.append({
                "account_id": acct_id,
                "label": label,
                "percentage": float(pct),
            })
        i += 1

    payload: dict = {}
    if form.get("name", "").strip():
        payload["name"] = form["name"].strip()
    payload["description"] = form.get("description", "").strip() or None
    if form.get("source_account_id", "").strip():
        payload["source_account_id"] = form["source_account_id"].strip()
    if targets:
        payload["targets"] = targets
    payload["is_active"] = "is_active" in form_data

    version = form.get("version", "1")

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/allocation_rules/{rule_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Allocation rule updated."
        return RedirectResponse(url=f"/allocations/{rule_id}", status_code=303)

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p not in ("body",)]
                    field = str(field_parts[0]) if field_parts else "__all__"
                    errors[field] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    elif resp.status_code == 409:
        errors["__all__"] = "Version conflict — rule was modified by another request. Please reload."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts = await _fetch_accounts(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/edit.html",
        {"rule": form, "form": form, "errors": errors, "accounts": accounts},
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.post(
    "/allocations/{rule_id}/delete", response_class=HTMLResponse, response_model=None
)
async def allocation_delete(
    request: Request, rule_id: uuid.UUID
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", "1"))

    async with api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/allocation_rules/{rule_id}",
            headers={"If-Match": version},
        )

    if resp.status_code == 204:
        request.session["flash"] = "Allocation rule archived."
    elif resp.status_code == 409:
        request.session["flash"] = "Could not archive — version conflict. Please reload."
    return RedirectResponse(url="/allocations", status_code=303)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@router.post(
    "/allocations/{rule_id}/apply", response_class=HTMLResponse, response_model=None
)
async def allocation_apply(
    request: Request, rule_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    payload = {
        "entry_date": str(form_data.get("entry_date", "")),
        "amount": str(form_data.get("amount", "")),
        "description": str(form_data.get("description", "")).strip() or None,
    }

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/allocation_rules/{rule_id}/apply",
            json=payload,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        je_id = resp.json().get("journal_entry_id", "")
        request.session["flash"] = f"Allocation applied — JE {je_id} posted."
        return RedirectResponse(url=f"/allocations/{rule_id}", status_code=303)

    # Error — re-render detail with error
    error_msg = f"Apply failed: HTTP {resp.status_code}"
    try:
        detail = resp.json().get("detail", "")
        if detail:
            error_msg = f"Apply failed: {detail}"
    except Exception:
        pass

    # Re-fetch rule for re-render
    rule: dict = {}
    async with api_client(request) as client:
        rresp = await client.get(f"/api/v1/allocation_rules/{rule_id}")
    if rresp.is_success:
        rule = rresp.json()

    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/detail.html",
        {
            "rule": rule,
            "flash": None,
            "apply_error": error_msg,
            "apply_form": dict(form_data),
        },
        status_code=422,
    )


# ---------------------------------------------------------------------------
# Detail  (catch-all — MUST be last)
# ---------------------------------------------------------------------------


@router.get("/allocations/{rule_id}", response_class=HTMLResponse, response_model=None)
async def allocation_detail(
    request: Request, rule_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    flash = request.session.pop("flash", None)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/allocation_rules/{rule_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        return RedirectResponse(url="/allocations", status_code=303)

    rule = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "allocations/detail.html",
        {"rule": rule, "flash": flash, "apply_error": None, "apply_form": {}},
    )
