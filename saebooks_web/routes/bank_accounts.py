"""Bank accounts list, detail, create, edit, archive — Lane D cycles 27 + 35.

GET  /bank-accounts             — list page (paginated, HTMX-aware)
GET  /bank-accounts/new         — empty create form; generates idempotency key
POST /bank-accounts/new         — submit to upstream API; redirect on success,
                                  re-render with errors on 422
GET  /bank-accounts/{id}/edit   — pre-populated edit form (version in hidden input)
                                  If account is archived -> 422 + edit_blocked.html
POST /bank-accounts/{id}/edit   — submit PATCH to API with If-Match; redirect on
                                  success, re-render on 409 (conflict) or 422
POST /bank-accounts/{id}/archive — soft-archive via archive_entity helper
GET  /bank-accounts/{id}        — bank account detail

Route ordering: /new + /{id}/edit + /{id}/archive MUST appear before the
catch-all /{id} GET so FastAPI matches literal paths first.

Auth guard: redirect to /login (303) if no session token.

The API uses page/page_size pagination and the prefix is /api/v1/bank_accounts.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.archive_helpers import archive_entity as _archive_entity

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Fields present on both create and edit forms.
_EDIT_FIELDS = (
    "code",
    "name",
    "account_kind",
    "bsb",
    "bank_account_number",
    "bank_account_title",
    "bank_abbreviation",
    "apca_user_id",
)

# Display order + label for the Bank Accounts list sections.
_KIND_SECTIONS: tuple[tuple[str, str], ...] = (
    ("BANK_CHECKING", "Bank — Checking"),
    ("BANK_SAVINGS",  "Bank — Savings"),
    ("CREDIT_CARD",   "Credit Cards"),
    ("BANK_LOAN",     "Loans"),
    ("CASH",          "Cash"),
    ("OTHER",         "Other"),
)


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/bank-accounts", response_class=HTMLResponse, response_model=None)
async def bank_accounts_list(
    request: Request,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the bank accounts list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``bank_accounts/_table.html`` partial only.  Otherwise the full page
    (``bank_accounts/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # The API uses page/page_size.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if archived:
        params["archived"] = True

    error: str | None = None
    accounts: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank_accounts", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            accounts = payload.get("items", [])
            total = payload.get("total", len(accounts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    # Bucket accounts by ``account_kind`` so the list renders as sections.
    # Buckets preserve _KIND_SECTIONS ordering; any kind not in the canon
    # falls into OTHER. Accounts with no kind set (legacy rows that didn't
    # get auto-classified) also land in OTHER so they remain visible.
    by_kind: dict[str, list[dict]] = {k: [] for k, _ in _KIND_SECTIONS}
    for a in accounts:
        k = a.get("account_kind") or "OTHER"
        by_kind.setdefault(k, by_kind["OTHER"])
        by_kind[k].append(a) if k in by_kind else by_kind["OTHER"].append(a)
    sections = [
        {"key": k, "label": label, "items": by_kind.get(k, [])}
        for k, label in _KIND_SECTIONS
        if by_kind.get(k)
    ]

    ctx = {
        "accounts": accounts,
        "sections": sections,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_archived": archived,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "bank_accounts/_table.html" if is_htmx else "bank_accounts/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: MUST appear before /{account_id} to win the literal-path match.
# ---------------------------------------------------------------------------


@router.get("/bank-accounts/new", response_class=HTMLResponse, response_model=None)
async def bank_account_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-bank-account form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_accounts/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
        },
    )


@router.post("/bank-accounts/new", response_class=HTMLResponse, response_model=None)
async def bank_account_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-bank-account form.

    - 201 -> 303 redirect to /bank-accounts/{id}
    - 422 -> re-render form with per-field errors (or __all__ for string errors)
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Required fields always included.
    payload: dict[str, object] = {}
    for required_field in ("code", "name"):
        val = form.get(required_field, "").strip()
        payload[required_field] = val

    # account_kind drives the form shape; default to BANK_CHECKING.
    payload["account_kind"] = (form.get("account_kind", "").strip()
                               or "BANK_CHECKING")

    # Optional fields — include only when non-empty. BSB is now optional
    # because credit cards and loans don't carry one.
    for optional_field in (
        "bsb",
        "bank_account_number",
        "bank_account_title",
        "bank_abbreviation",
        "apca_user_id",
    ):
        val = form.get(optional_field, "").strip()
        if val:
            payload[optional_field] = val

    # Boolean checkbox — HTML sends "on" when checked, nothing when unchecked.
    payload["is_trust_account"] = form.get("is_trust_account") == "on"

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/bank_accounts",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/bank-accounts/{created['id']}", status_code=303)

    # 422 — parse per-field or plain-string errors.
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

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_accounts/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: MUST appear before /{account_id} catch-all.
# ---------------------------------------------------------------------------


@router.get(
    "/bank-accounts/{account_id}/edit", response_class=HTMLResponse, response_model=None
)
async def bank_account_edit_form(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing bank account.

    If the account is already archived, renders edit_blocked.html with HTTP 422.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_accounts/{account_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_accounts/edit.html",
            {
                "account": None,
                "form": {},
                "errors": {"__all__": "Bank account not found"},
                "conflict": False,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_accounts/edit.html",
            {
                "account": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
            },
            status_code=resp.status_code,
        )

    account = resp.json()

    # Block editing archived accounts.
    if account.get("archived_at"):
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_accounts/edit_blocked.html",
            {"account": account},
            status_code=422,
        )

    form: dict[str, str] = {field: str(account.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(account.get("version", ""))
    form["is_trust_account"] = "on" if account.get("is_trust_account") else ""

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_accounts/edit.html",
        {
            "account": account,
            "form": form,
            "errors": {},
            "conflict": False,
        },
    )


@router.post(
    "/bank-accounts/{account_id}/edit", response_class=HTMLResponse, response_model=None
)
async def bank_account_update(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    - 200 OK       -> 303 redirect to /bank-accounts/{id}
    - 409 Conflict -> re-fetch latest, re-render with conflict banner + server version
    - 422          -> re-render with per-field validation errors
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    # Build PATCH payload — only include non-empty fields.
    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    # Boolean checkbox — always included so unchecking clears trust designation.
    payload["is_trust_account"] = form.get("is_trust_account") == "on"

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/bank_accounts/{account_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Bank account saved."
        return RedirectResponse(url=f"/bank-accounts/{account_id}", status_code=303)

    # 409 Conflict — re-fetch server's latest, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/bank_accounts/{account_id}")

        server_account: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_account.get("version", ""))

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        return _TEMPLATES.TemplateResponse(
            request,
            "bank_accounts/edit.html",
            {
                "account": server_account,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_account": server_account,
            },
            status_code=409,
        )

    # 422 — per-field validation errors.
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
    elif resp.status_code == 428:
        import logging as _logging

        _logging.getLogger(__name__).error(
            "PATCH /api/v1/bank_accounts/%s returned 428 — If-Match header was missing",
            account_id,
        )
        errors["__all__"] = (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "bank_accounts/edit.html",
        {
            "account": None,
            "form": form,
            "errors": errors,
            "conflict": False,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Archive — POST /{account_id}/archive
# NOTE: MUST appear before the catch-all /{account_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/bank-accounts/{account_id}/archive", response_class=HTMLResponse, response_model=None
)
async def bank_account_archive(
    request: Request,
    account_id: str,
) -> RedirectResponse:
    """Soft-archive a bank account via DELETE /api/v1/bank_accounts/{id} with If-Match.

    On success redirects to /bank-accounts with a flash.
    On 409 (version conflict) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/bank_accounts",
        entity_id=account_id,
        version=str(version),
        entity_label=f"Bank account {account_id}",
        list_url="/bank-accounts",
        detail_url=f"/bank-accounts/{account_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/bank-accounts/{account_id}", response_class=HTMLResponse, response_model=None)
async def bank_account_detail(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single bank account detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_accounts/{account_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_accounts/detail.html",
                {"account": None, "error": "Bank account not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_accounts/detail.html",
                {"account": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    account = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "bank_accounts/detail.html",
        {"account": account, "error": None, "flash": flash},
    )
