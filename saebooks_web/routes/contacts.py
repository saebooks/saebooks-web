"""Contacts list, detail, create and edit views — Lane D cycles 1, 7 + 8.

GET  /contacts              — list page (paginated, first 100)
GET  /contacts/new          — empty create form; generates idempotency key
POST /contacts/new          — submit to upstream API; redirect on success,
                              re-render with errors on 422
GET  /contacts/{id}         — contact detail
GET  /contacts/{id}/edit    — pre-populated edit form (version in hidden input)
POST /contacts/{id}/edit    — submit PATCH to API with If-Match; redirect on
                              success, re-render on 409 (conflict) or 422

Route ordering: /contacts/new MUST be declared before /contacts/{contact_id}
so FastAPI matches the literal path first.

HTMX extension points (TODO — future cycles):
- Pagination via hx-get with ?offset= query param, swapping the table body
- Inline search with hx-trigger="keyup changed delay:300ms"
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


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


_CONTACT_TYPE_VALUES = {"CUSTOMER", "SUPPLIER", "BOTH", "BENEFICIARY"}


@router.get("/contacts", response_class=HTMLResponse, response_model=None)
async def contacts_list(
    request: Request,
    show: str | None = None,
    contact_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the contacts list page.

    By default, one-off contacts (``is_one_off=true``) are hidden.
    ``?show=one-off`` lists ONLY the one-offs; ``?show=all`` lists
    everything. Anything else (or unset) renders the main pool.

    ``?contact_type=CUSTOMER|SUPPLIER|BOTH|BENEFICIARY`` filters by the
    underlying ContactType enum.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    contacts: list[dict] = []
    total: int = 0

    ct = (contact_type or "").upper().strip()
    if ct not in _CONTACT_TYPE_VALUES:
        ct = ""

    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if ct:
        # The API list endpoint exposes the ContactType filter as ?type=
        # (alias on Query(default=None, alias="type")). Sending the field's
        # internal name "contact_type" is silently ignored.
        params["type"] = ct

    async with api_client(request) as client:
        resp = await client.get("/api/v1/contacts", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            contacts = payload.get("items", [])
            total = payload.get("total", len(contacts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Also pull the one-off candidate count so we can render a nudge
    # banner on the main view ("8 contacts look like one-offs — review")
    candidate_count = 0
    if show != "one-off":
        try:
            async with api_client(request) as client:
                cr = await client.get("/api/v1/contacts/one-off-candidates")
                if cr.is_success:
                    candidate_count = cr.json().get("total", 0)
        except Exception:
            pass

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/list.html",
        {
            "contacts": contacts,
            "total": total,
            "error": error,
            "flash": flash,
            "show": show or "",
            "filter_contact_type": ct,
            "limit": limit,
            "offset": offset,
            "candidate_count": candidate_count,
        },
    )


# ---------------------------------------------------------------------------
# One-off cleanup — review screen + bulk-tag POST
# ---------------------------------------------------------------------------


@router.get("/contacts/cleanup", response_class=HTMLResponse, response_model=None)
async def contacts_cleanup_review(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Render the one-off-contact review screen.

    Lists contacts that look like one-offs (one POSTED transaction, no
    drafts, quiet for >=90 days) with checkboxes pre-ticked. Submitting
    POSTs to the API's bulk-tag-one-off endpoint and flips them.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    candidates: list[dict] = []
    error: str | None = None
    async with api_client(request) as client:
        cr = await client.get("/api/v1/contacts/one-off-candidates")
        if cr.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if cr.is_success:
            candidates = cr.json().get("items", [])
        else:
            error = f"API error: HTTP {cr.status_code}"

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/cleanup.html",
        {"candidates": candidates, "error": error, "flash": flash},
    )


@router.post("/contacts/cleanup", response_class=HTMLResponse, response_model=None)
async def contacts_cleanup_apply(request: Request) -> RedirectResponse:
    """Apply the selected one-off tags via /api/v1/contacts/bulk-tag-one-off."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    selected = [v for k, v in form.multi_items() if k == "contact_id"]
    if not selected:
        request.session["flash"] = "No contacts selected — nothing changed."
        return RedirectResponse(url="/contacts/cleanup", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/contacts/bulk-tag-one-off",
            json={"contact_ids": selected},
        )
    if resp.is_success:
        flipped = resp.json().get("flipped", 0)
        request.session["flash"] = f"Marked {flipped} contact{'s' if flipped != 1 else ''} as one-off."
    else:
        request.session["flash"] = f"Bulk tag failed: HTTP {resp.status_code}"
    return RedirectResponse(url="/contacts", status_code=303)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before the /{contact_id} route so FastAPI
# resolves /contacts/new as a literal path rather than a path parameter.
# ---------------------------------------------------------------------------


async def _fetch_contact_dropdowns(request: Request) -> tuple[list[dict], list[dict]]:
    """Fetch accounts and tax codes for the contact form dropdowns.

    Returns empty lists on any API error so the form degrades gracefully.
    """
    accounts: list[dict] = []
    tax_codes: list[dict] = []
    async with api_client(request) as client:
        a_resp = await client.get("/api/v1/accounts", params={"limit": 1000, "offset": 0})
        if a_resp.is_success:
            accounts = a_resp.json().get("items", [])
        t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
        if t_resp.is_success:
            tax_codes = t_resp.json().get("items", [])
    return accounts, tax_codes


@router.get("/contacts/new", response_class=HTMLResponse, response_model=None)
async def contact_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-contact form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    accounts, tax_codes = await _fetch_contact_dropdowns(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/new.html",
        {
            "form": {},
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "accounts": accounts,
            "tax_codes": tax_codes,
        },
    )


@router.post("/contacts/new", response_class=HTMLResponse, response_model=None)
async def contact_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-contact form.

    Calls POST /api/v1/contacts on the upstream API.
    - 201 -> 303 redirect to /contacts/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with per-field errors + submitted values preserved
    - 401 -> clear session, redirect to /login
    - other errors -> re-render form with a generic error message
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    # Collect the raw submitted values for re-display on validation failure.
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the payload — only send non-empty optional fields.
    payload: dict[str, object] = {}
    for field in (
        "name",
        "contact_type",
        "email",
        "phone",
        "abn",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postcode",
        "country",
        "notes",
        "default_tax_code",
        "bank_bsb",
        "bank_account_number",
        "bank_account_title",
        "default_account_id",
        "currency_code",
    ):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["is_one_off"] = form.get("is_one_off") == "on"

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/contacts",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/contacts/{created['id']}", status_code=303)

    # 422 — parse per-field validation errors from FastAPI/Pydantic detail array.
    errors: dict[str, str] = {}
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail", [])
            if isinstance(detail, list):
                for err in detail:
                    # Pydantic v2 location: ["body", "field_name"] or ["body", "field_name", ...]
                    loc = err.get("loc", [])
                    # Strip the leading "body" segment if present.
                    field_parts = [p for p in loc if p != "body"]
                    field = str(field_parts[0]) if field_parts else "__all__"
                    errors[field] = err.get("msg", "Invalid value")
            elif isinstance(detail, str):
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts, tax_codes = await _fetch_contact_dropdowns(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "accounts": accounts,
            "tax_codes": tax_codes,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match)
# NOTE: these routes MUST appear before /contacts/{contact_id} for the same
# literal-vs-parameter ordering reason as /contacts/new.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = (
    "name",
    "contact_type",
    "email",
    "phone",
    "abn",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postcode",
    "country",
    "notes",
    "default_tax_code",
    "bank_bsb",
    "bank_account_number",
    "bank_account_title",
    "default_account_id",
    "currency_code",
)


@router.get("/contacts/{contact_id}/edit", response_class=HTMLResponse, response_model=None)
async def contact_edit_form(
    request: Request,
    contact_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing contact.

    Fetches the current record from the API and renders edit.html with all
    fields pre-filled. The current ``version`` is stored in a hidden input
    so the subsequent POST can include it in the ``If-Match`` header.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/contacts/{contact_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "contacts/edit.html",
            {
                "contact": None,
                "form": {},
                "errors": {"__all__": "Contact not found"},
                "conflict": False,
                "accounts": [],
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "contacts/edit.html",
            {
                "contact": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "accounts": [],
            },
            status_code=resp.status_code,
        )

    contact = resp.json()
    # Pre-populate the form dict from the API response.
    form: dict[str, str] = {field: str(contact.get(field) or "") for field in _EDIT_FIELDS}
    form["version"] = str(contact.get("version", ""))

    accounts, tax_codes = await _fetch_contact_dropdowns(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/edit.html",
        {
            "contact": contact,
            "form": form,
            "errors": {},
            "conflict": False,
            "accounts": accounts,
            "tax_codes": tax_codes,
        },
    )


@router.post("/contacts/{contact_id}/edit", response_class=HTMLResponse, response_model=None)
async def contact_update(
    request: Request,
    contact_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with an If-Match header.

    Outcomes:
    - 200 OK      → 303 redirect to /contacts/{id}  (Post-Redirect-Get)
    - 409 Conflict → re-fetch latest record, re-render form with a conflict
                     banner and the server's current version in the hidden
                     input.  The user's submitted values are preserved so
                     they can compare and re-submit.
    - 422          → re-render with per-field validation errors
    - 428          → If-Match missing (shouldn't happen; log + generic error)
    - 401          → clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")

    # Build the PATCH payload — only include non-empty fields.
    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    # is_one_off is a checkbox — POST it on every edit (no "leave alone"
    # semantics; the edit form is the source of truth for the flag).
    payload["is_one_off"] = form.get("is_one_off") == "on"

    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/contacts/{contact_id}",
            json=payload,
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        return RedirectResponse(url=f"/contacts/{contact_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input,
    # and show a conflict banner so the user can reconcile their changes.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/contacts/{contact_id}")

        server_contact: dict = latest_resp.json() if latest_resp.is_success else {}
        server_version = str(server_contact.get("version", ""))

        # Keep user's submitted form values but update the hidden version to
        # the server's current version so the next submit has a fighting chance.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        accounts, tax_codes = await _fetch_contact_dropdowns(request)

        return _TEMPLATES.TemplateResponse(
            request,
            "contacts/edit.html",
            {
                "contact": server_contact,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_contact": server_contact,
                "accounts": accounts,
                "tax_codes": tax_codes,
            },
            status_code=409,
        )

    # 422 — parse per-field validation errors.
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
            "PATCH /api/v1/contacts/%s returned 428 — If-Match header was missing",
            contact_id,
        )
        errors["__all__"] = "Precondition required: version information was missing. Please reload and try again."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    accounts, tax_codes = await _fetch_contact_dropdowns(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/edit.html",
        {
            "contact": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "accounts": accounts,
            "tax_codes": tax_codes,
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )



# ---------------------------------------------------------------------------
# Per-row one-off toggle — POST /{contact_id}/make-one-off | /make-main
# NOTE: MUST appear before the catch-all /{contact_id} GET so FastAPI matches
# the literal "/make-one-off" / "/make-main" suffixes first.
# ---------------------------------------------------------------------------


async def _set_one_off(request: Request, contact_id: str, *, value: bool) -> RedirectResponse:
    """Helper: flip ``is_one_off`` on a single contact via the bulk API.

    Uses /api/v1/contacts/bulk-tag-one-off with a single-element list so the
    server handles the fetch + If-Match + change_log internally. Avoids the
    round-trip-and-version-juggling the per-row PATCH would need.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/contacts/bulk-tag-one-off",
            json={"contact_ids": [contact_id], "is_one_off": value},
        )
    label = "one-off" if value else "main"
    if resp.is_success:
        flipped = resp.json().get("flipped", 0)
        if flipped:
            request.session["flash"] = f"Marked as {label}."
        else:
            request.session["flash"] = f"Already {label} — no change."
    else:
        request.session["flash"] = f"Could not update: HTTP {resp.status_code}"
    # Stay on whichever list view the user came from when possible.
    referer = request.headers.get("referer", "")
    target = "/contacts"
    if referer:
        # Trust only same-host paths to avoid open-redirect
        from urllib.parse import urlparse
        try:
            parsed = urlparse(referer)
            if parsed.path.startswith("/contacts"):
                target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        except Exception:
            pass
    return RedirectResponse(url=target, status_code=303)


@router.post("/contacts/{contact_id}/make-one-off", response_class=HTMLResponse, response_model=None)
async def contact_make_one_off(request: Request, contact_id: str) -> RedirectResponse:
    """Flip a single contact's ``is_one_off`` flag to True."""
    return await _set_one_off(request, contact_id, value=True)


@router.post("/contacts/{contact_id}/make-main", response_class=HTMLResponse, response_model=None)
async def contact_make_main(request: Request, contact_id: str) -> RedirectResponse:
    """Flip a single contact's ``is_one_off`` flag back to False."""
    return await _set_one_off(request, contact_id, value=False)


# ---------------------------------------------------------------------------
# Archive — POST /{contact_id}/archive
# NOTE: MUST appear before the catch-all /{contact_id} GET.
# Contacts have no status field.  Archive is available whenever archived_at is None.
# ---------------------------------------------------------------------------


@router.post(
    "/contacts/{contact_id}/archive", response_class=HTMLResponse, response_model=None
)
async def contact_archive(
    request: Request,
    contact_id: str,
) -> RedirectResponse:
    """Soft-archive a contact via DELETE /api/v1/contacts/{id} with If-Match.

    Contacts have no status field — archive is always available unless archived_at
    is already set.  On success redirects to /contacts with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/contacts",
        entity_id=contact_id,
        version=str(version),
        entity_label=f"Contact {contact_id}",
        list_url="/contacts",
        detail_url=f"/contacts/{contact_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


_TXN_SORT_KEYS = {"date", "type", "number", "amount", "status"}


def _txn_sort_key(t: dict, key: str) -> object:
    """Return a comparable value for sorting transactions on ``key``."""
    if key == "amount":
        try:
            return float(t.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0
    return str(t.get(key) or "")


@router.get("/contacts/{contact_id}", response_class=HTMLResponse, response_model=None)
async def contact_detail(
    request: Request,
    contact_id: str,
    txn_type: str | None = None,
    txn_status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "date",
    direction: str = "desc",
) -> HTMLResponse | RedirectResponse:
    """Render a single contact detail page with their transaction history.

    Fans out to /api/v1/{invoices,bills,payments,credit_notes,expenses}
    filtered by ``contact_id``, merges results into one chronological list,
    and applies the requested filter + sort. Each transaction row carries
    enough metadata to deep-link back to its underlying detail page.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    sort = sort if sort in _TXN_SORT_KEYS else "date"
    direction = "asc" if direction == "asc" else "desc"

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/contacts/{contact_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "contacts/detail.html",
                {"contact": None, "error": "Contact not found", "flash": None,
                 "transactions": [], "txn_type": "", "txn_status": "",
                 "date_from": "", "date_to": "", "sort": sort, "direction": direction},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "contacts/detail.html",
                {"contact": None, "error": f"API error: HTTP {resp.status_code}", "flash": None,
                 "transactions": [], "txn_type": "", "txn_status": "",
                 "date_from": "", "date_to": "", "sort": sort, "direction": direction},
                status_code=resp.status_code,
            )

        contact = resp.json()

        params: dict[str, object] = {
            "contact_id": contact_id,
            "page": 1,
            "page_size": 500,
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to

        async def _safe_list(path: str, key: str = "items") -> list[dict]:
            try:
                r = await client.get(path, params=params)
                if r.is_success:
                    return r.json().get(key, []) or []
            except Exception:
                pass
            return []

        invoices = await _safe_list("/api/v1/invoices")
        bills = await _safe_list("/api/v1/bills")
        payments = await _safe_list("/api/v1/payments")
        credit_notes = await _safe_list("/api/v1/credit_notes")
        expenses = await _safe_list("/api/v1/expenses")

    # Normalise each source into one shape:
    #   {type, date, number, ref, amount, status, url, id}
    transactions: list[dict] = []
    for inv in invoices:
        transactions.append({
            "type": "Invoice",
            "date": inv.get("issue_date") or "",
            "number": inv.get("number") or "(draft)",
            "ref": inv.get("reference") or "",
            "amount": inv.get("total") or "0",
            "status": inv.get("status") or "",
            "url": f"/invoices/{inv['id']}",
            "id": inv["id"],
        })
    for bill in bills:
        transactions.append({
            "type": "Bill",
            "date": bill.get("issue_date") or "",
            "number": bill.get("number") or "(draft)",
            "ref": bill.get("supplier_reference") or "",
            "amount": bill.get("total") or "0",
            "status": bill.get("status") or "",
            "url": f"/bills/{bill['id']}",
            "id": bill["id"],
        })
    for pay in payments:
        transactions.append({
            "type": "Payment",
            "date": pay.get("payment_date") or pay.get("date") or "",
            "number": pay.get("number") or pay.get("reference") or "",
            "ref": pay.get("reference") or "",
            "amount": pay.get("amount") or "0",
            "status": pay.get("status") or "",
            "url": f"/payments/{pay['id']}",
            "id": pay["id"],
        })
    for cn in credit_notes:
        transactions.append({
            "type": "Credit Note",
            "date": cn.get("issue_date") or "",
            "number": cn.get("number") or "(draft)",
            "ref": cn.get("reference") or "",
            "amount": cn.get("total") or "0",
            "status": cn.get("status") or "",
            "url": f"/credit_notes/{cn['id']}",
            "id": cn["id"],
        })
    for exp in expenses:
        transactions.append({
            "type": "Expense",
            "date": exp.get("expense_date") or "",
            "number": exp.get("number") or "(draft)",
            "ref": exp.get("reference") or "",
            "amount": exp.get("total") or "0",
            "status": exp.get("status") or "",
            "url": f"/expenses/{exp['id']}",
            "id": exp["id"],
        })

    if txn_type:
        transactions = [t for t in transactions if t["type"].lower() == txn_type.lower()]
    if txn_status:
        transactions = [t for t in transactions if t["status"].upper() == txn_status.upper()]

    transactions.sort(
        key=lambda t: _txn_sort_key(t, sort),
        reverse=(direction == "desc"),
    )

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/detail.html",
        {"contact": contact, "error": None, "flash": flash,
         "transactions": transactions,
         "txn_type": txn_type or "",
         "txn_status": txn_status or "",
         "date_from": date_from or "",
         "date_to": date_to or "",
         "sort": sort,
         "direction": direction},
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /contacts/bulk
# ---------------------------------------------------------------------------

# Per-row actions: iterate ids, dispatch one HTTP call per id.
_BULK_ACTIONS_CONTACTS = {
    "archive": ("DELETE", "/api/v1/contacts/{id}"),
}

# Single-call actions: one HTTP request, ids carried in the body.
# Tuple: (method, path, base_body — ids are merged in at dispatch time).
_BULK_ACTIONS_CONTACTS_SINGLE: dict[str, tuple[str, str, dict]] = {
    "make_one_off": ("POST", "/api/v1/contacts/bulk-tag-one-off", {"is_one_off": True}),
    "make_main":    ("POST", "/api/v1/contacts/bulk-tag-one-off", {"is_one_off": False}),
}


@router.post("/contacts/bulk", response_class=HTMLResponse, response_model=None)
async def contacts_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many contacts at once.

    Form fields:
      action  — one of: archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /contacts. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_CONTACTS and action not in _BULK_ACTIONS_CONTACTS_SINGLE:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/contacts", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/contacts", status_code=303)

    # ── Single-call actions (one HTTP call, all ids in body) ────────────────
    if action in _BULK_ACTIONS_CONTACTS_SINGLE:
        method, path, base_body = _BULK_ACTIONS_CONTACTS_SINGLE[action]
        body = {**base_body, "contact_ids": ids}
        async with api_client(request) as client:
            resp = await client.request(method, path, json=body)
        label = action.replace("_", " ").title()
        if resp.is_success:
            flipped = resp.json().get("flipped", 0)
            skipped = len(ids) - flipped
            tail = f" ({skipped} already in target state)" if skipped else ""
            request.session["flash"] = f"{label}: {flipped} contact{'s' if flipped != 1 else ''} updated{tail}."
        else:
            request.session["flash"] = f"{label} failed: HTTP {resp.status_code}"
        return RedirectResponse(url="/contacts", status_code=303)

    method, path_tpl = _BULK_ACTIONS_CONTACTS[action]
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
        request.session["flash"] = f"{label}: {ok} contact{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/contacts", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/contacts/{contact_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def contact_hard_delete(request: Request, contact_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/contacts",
        entity_id=contact_id,
        entity_label=f"Contact {contact_id}",
        list_url="/contacts",
        detail_url=f"/contacts/{contact_id}",
    )
