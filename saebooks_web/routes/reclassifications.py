"""Reclassifications list, detail, and create views — the sanctioned
no-manual-JE correction path.

GET  /reclassifications               — list page (paginated, HTMX-aware)
GET  /reclassifications/new           — empty create form
POST /reclassifications/new           — submit to upstream API; redirect on
                                        success, re-render with errors on 400
GET  /reclassifications/{id}          — reclassification detail
POST /reclassifications/{id}/reverse  — reverse a POSTED reclassification

A reclassification moves an already-posted amount from one account to another
by posting ONE balanced, engine-generated journal entry — without mutating the
original record. It exists precisely so users do NOT reach for a manual
journal entry to fix a mis-coded transaction. See engine
``saebooks/api/v1/reclassifications.py``.

Follows the transfers pattern (the engine surface mirrors it):
- No draft/post lifecycle — ``POST /reclassifications`` creates AND posts in
  one call. No edit route, no If-Match.
- ``ReclassificationListOut`` is ``{"items": [...]}`` — no ``total`` field, so
  pagination is "does this page look full".
- Both accounts must sit on the SAME natural balance side (expense->expense,
  asset->asset, income->income, liability->liability) — the engine enforces
  this; the form offers every account and surfaces the 400 message.
- Business-rule failures: HTTP 400 with
  ``{"detail": {"code": "reclassification_invalid", "detail": "<message>"}}``.
- Two actions only: create and reverse (409 when already reversed).

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import uuid
from datetime import date
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


def _parse_error_detail(resp) -> str:
    """Extract a human-readable message from the engine's error shapes.

    Reclassification business-rule failures nest the message under
    ``detail.detail`` (``{"code": "reclassification_invalid", "detail":
    "..."}``); fall back gracefully for any other shape.
    """
    try:
        body = resp.json()
    except Exception:
        return f"API error: HTTP {resp.status_code}"
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("detail") or detail.get("code") or detail)
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            return str(first.get("msg", first))
        return str(first)
    if isinstance(detail, str):
        return detail
    return f"API error: HTTP {resp.status_code}"


async def _fetch_accounts(client) -> list[dict]:
    """Return every account of the active company, sorted by code.

    A reclassification can pair any two accounts on the same natural balance
    side — the engine enforces the pairing, so the form offers the full chart
    rather than second-guessing which sides match.
    """
    resp = await client.get(
        "/api/v1/accounts", params={"limit": 1000, "offset": 0}
    )
    if not resp.is_success:
        return []
    accounts = resp.json().get("items", [])
    accounts.sort(key=lambda a: a.get("code", ""))
    return accounts


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/reclassifications", response_class=HTMLResponse, response_model=None)
async def reclassifications_list(
    request: Request,
    account_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the reclassifications list page (full or HTMX fragment).

    ``ReclassificationListOut`` carries no ``total`` — pagination is "next
    page exists if this page came back full", not a true page count.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if account_id:
        params["account_id"] = account_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    reclassifications: list[dict] = []
    accounts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/reclassifications", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            reclassifications = resp.json().get("items", [])
        else:
            error = _parse_error_detail(resp)

        for acct in await _fetch_accounts(client):
            accounts_by_id[acct["id"]] = acct

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    # No total to compare against — assume more exist if this page is full.
    next_offset = offset + limit if len(reclassifications) == limit else None

    flash = request.session.pop("flash", None)

    ctx = {
        "reclassifications": reclassifications,
        "accounts_by_id": accounts_by_id,
        "error": error,
        "flash": flash,
        "filter_account_id": account_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = (
        "reclassifications/_table.html" if is_htmx else "reclassifications/list.html"
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /reclassifications/{id} so FastAPI
# resolves the literal path first.
# ---------------------------------------------------------------------------


@router.get("/reclassifications/new", response_class=HTMLResponse, response_model=None)
async def reclassification_new_form(
    request: Request,
    from_account_id: str | None = None,
    source_entry_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the empty create form.

    ``from_account_id`` and ``source_entry_id`` may arrive as query params so
    other pages (a journal-entry detail, an account ledger) can deep-link a
    pre-filled correction.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        accounts = await _fetch_accounts(client)

    form: dict[str, str] = {"reclass_date": today}
    if from_account_id:
        form["from_account_id"] = from_account_id
    if source_entry_id:
        form["source_entry_id"] = source_entry_id

    return _TEMPLATES.TemplateResponse(
        request,
        "reclassifications/new.html",
        {
            "form": form,
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
        },
    )


@router.post("/reclassifications/new", response_class=HTMLResponse, response_model=None)
async def reclassification_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create form.

    Calls POST /api/v1/reclassifications — creates AND posts in one call.
    201 -> 303 redirect to /reclassifications/{id}. 400 (business-rule
    rejection, e.g. accounts on different natural sides) -> re-render with
    the engine's message. 422 (pydantic validation) -> re-render with
    per-field errors.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in (
        "from_account_id",
        "to_account_id",
        "reclass_date",
        "reason",
        "source_entry_id",
        "override_reason",
    ):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val
    amount = form.get("amount", "").strip()
    if amount:
        payload["amount"] = amount

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/reclassifications",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        request.session["flash"] = "Reclassification recorded and posted."
        return RedirectResponse(
            url=f"/reclassifications/{created['id']}", status_code=303
        )

    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    loc = err.get("loc", [])
                    field_parts = [p for p in loc if p != "body"]
                    field_key = str(field_parts[0]) if field_parts else "__all__"
                    errors[field_key] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = _parse_error_detail(resp)

    async with api_client(request) as client:
        accounts = await _fetch_accounts(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "reclassifications/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "accounts": accounts,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Reverse — POST /{id}/reverse
# NOTE: MUST appear before the catch-all /{id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/reclassifications/{reclassification_id}/reverse",
    response_class=HTMLResponse,
    response_model=None,
)
async def reclassification_reverse(
    request: Request, reclassification_id: str
) -> RedirectResponse:
    """Reverse a POSTED reclassification.

    POSTs to POST /api/v1/reclassifications/{id}/reverse — no If-Match (the
    engine doesn't version this record type).
    - 200 -> 303 to detail with flash "Reclassification reversed."
    - 409 -> already reversed / not reversible -> flash the engine message
    - 404 -> flash "Reclassification not found."
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/reclassifications/{reclassification_id}/reverse", json={}
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Reclassification reversed."
    elif resp.status_code == 404:
        request.session["flash"] = "Reclassification not found."
        return RedirectResponse(url="/reclassifications", status_code=303)
    else:
        request.session["flash"] = _parse_error_detail(resp)

    return RedirectResponse(
        url=f"/reclassifications/{reclassification_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get(
    "/reclassifications/{reclassification_id}",
    response_class=HTMLResponse,
    response_model=None,
)
async def reclassification_detail(
    request: Request, reclassification_id: str
) -> HTMLResponse | RedirectResponse:
    """Render a single reclassification detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/reclassifications/{reclassification_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "reclassifications/detail.html",
                {"reclassification": None, "error": "Reclassification not found",
                 "flash": None, "from_account": None, "to_account": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "reclassifications/detail.html",
                {"reclassification": None, "error": _parse_error_detail(resp),
                 "flash": None, "from_account": None, "to_account": None},
                status_code=resp.status_code,
            )

        reclassification = resp.json()

        from_account = None
        to_account = None
        fa_resp = await client.get(
            f"/api/v1/accounts/{reclassification['from_account_id']}"
        )
        if fa_resp.is_success:
            from_account = fa_resp.json()
        ta_resp = await client.get(
            f"/api/v1/accounts/{reclassification['to_account_id']}"
        )
        if ta_resp.is_success:
            to_account = ta_resp.json()

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "reclassifications/detail.html",
        {
            "reclassification": reclassification,
            "error": None,
            "flash": flash,
            "from_account": from_account,
            "to_account": to_account,
        },
    )
