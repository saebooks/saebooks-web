"""Quotes list, detail, create, edit, and state-transition views.

GET  /quotes              — list page (paginated, HTMX-aware)
GET  /quotes/new          — empty create form; generates idempotency key
POST /quotes/new          — submit to upstream API; redirect on success,
                            re-render with errors on 422
GET  /quotes/_add_line    — HTMX partial: returns a single blank line row
GET  /quotes/{id}         — quote detail
GET  /quotes/{id}/edit    — edit form (DRAFT + SENT only)
POST /quotes/{id}/edit    — PATCH with If-Match
POST /quotes/{id}/send    — DRAFT → SENT
POST /quotes/{id}/accept  — SENT → ACCEPTED
POST /quotes/{id}/decline — SENT → DECLINED
POST /quotes/{id}/convert-to-invoice — ACCEPTED → INVOICED, returns invoice
POST /quotes/{id}/archive — archive (any non-INVOICED)

Route ordering: literal paths (/quotes/new, /quotes/_add_line) MUST appear
before /quotes/{quote_id} so FastAPI resolves them first.

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.form_helpers import parse_lines as _parse_lines_shared

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _parse_lines(form: dict[str, str]) -> list[dict[str, object]]:
    """Delegate to the shared helper in form_helpers.py."""
    return _parse_lines_shared(form)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/quotes", response_class=HTMLResponse, response_model=None)
async def quotes_list(
    request: Request,
    status: str | None = None,
    customer_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the quotes list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``quotes/_table.html`` partial only.  Otherwise the full page
    (``quotes/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if customer_id:
        params["customer_id"] = customer_id

    error: str | None = None
    quotes: list[dict] = []
    total: int = 0
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/quotes", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            quotes = payload.get("items", [])
            total = payload.get("total", len(quotes))
        else:
            error = f"API error: HTTP {resp.status_code}"

        c_resp = await client.get(
            "/api/v1/contacts",
            params={"contact_type": "CUSTOMER", "limit": 200, "offset": 0},
        )
        if c_resp.is_success:
            for c in c_resp.json().get("items", []):
                contacts_by_id[c["id"]] = c

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "quotes": quotes,
        "total": total,
        "error": error,
        "flash": flash,
        "contacts_by_id": contacts_by_id,
        "filter_status": status or "",
        "filter_customer_id": customer_id or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "quotes/_table.html" if is_htmx else "quotes/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: these routes MUST appear before /{quote_id} so FastAPI matches the
# literal paths first.
# ---------------------------------------------------------------------------


async def _fetch_quote_dropdowns(client) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch contacts (customers), accounts, tax_codes."""
    contacts: list[dict] = []
    accounts: list[dict] = []
    tax_codes: list[dict] = []

    c_resp = await client.get(
        "/api/v1/contacts",
        params={"contact_type": "CUSTOMER", "limit": 200, "offset": 0},
    )
    if c_resp.is_success:
        contacts = c_resp.json().get("items", [])

    a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if a_resp.is_success:
        accounts = a_resp.json().get("items", [])

    t_resp = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if t_resp.is_success:
        tax_codes = t_resp.json().get("items", [])

    return contacts, accounts, tax_codes


@router.get("/quotes/new", response_class=HTMLResponse, response_model=None)
async def quote_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-quote form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()
    expiry = (date.today() + timedelta(days=28)).isoformat()

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_quote_dropdowns(client)

    initial_lines = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "quotes/new.html",
        {
            "form": {
                "issue_date": today,
                "expiry_date": expiry,
                "deposit_pct": "50",
                "late_fee_pct_per_month": "2.5",
            },
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": initial_lines,
            "line_count": 1,
        },
    )


@router.post("/quotes/new", response_class=HTMLResponse, response_model=None)
async def quote_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-quote form.

    Calls POST /api/v1/quotes on the upstream API.
    - 201 -> 303 redirect to /quotes/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with per-field errors + submitted values preserved
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in ("customer_id", "issue_date", "expiry_date", "title", "notes", "terms"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    # Numeric quote-terms fields
    for field in ("deposit_pct", "late_fee_pct_per_month"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["is_supply_only"] = bool(form.get("is_supply_only"))
    payload["lines"] = _parse_lines(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/quotes",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/quotes/{created['id']}", status_code=303)

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
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_quote_dropdowns(client)

    raw_lines = _parse_lines(form)
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "quotes/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get("/quotes/{quote_id}/pdf", response_model=None)
async def quote_pdf(
    request: Request, quote_id: str
) -> Response | RedirectResponse:
    """Stream the rendered quote PDF from the API.

    The API serves the PDF inline (Content-Disposition: inline) — the link is
    `target="_blank"` on the detail page so the PDF opens in a new tab.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/quotes/{quote_id}/pdf")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        raise HTTPException(404, detail="Quote not found")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=f"Upstream returned {resp.status_code}")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/pdf"),
        headers={
            "Content-Disposition": resp.headers.get(
                "content-disposition", f'inline; filename="quote-{quote_id}.pdf"'
            ),
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


_FROM_OPTIONS = ("admin@saee.com.au", "accounts@saee.com.au")
_DEFAULT_FROM_BY_DOC_TYPE = {
    "quote":       "admin@saee.com.au",
    "invoice":     "accounts@saee.com.au",
    "bill":        "admin@saee.com.au",
    "credit_note": "accounts@saee.com.au",
    "remittance":  "accounts@saee.com.au",
    "letterhead":  "admin@saee.com.au",
}


@router.get("/quotes/{quote_id}/email", response_class=HTMLResponse, response_model=None)
async def quote_email_compose(
    request: Request, quote_id: str
) -> HTMLResponse | RedirectResponse:
    """Email composer for a quote — pre-fills To/Subject/Body from quote+customer."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        qresp = await client.get(f"/api/v1/quotes/{quote_id}")
        if qresp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if qresp.status_code == 404:
            raise HTTPException(404, "Quote not found")
        if qresp.status_code != 200:
            raise HTTPException(qresp.status_code, "Upstream error")
        quote = qresp.json()

        # Pull customer for default-To address
        cresp = await client.get(f"/api/v1/contacts/{quote['customer_id']}")
        customer = cresp.json() if cresp.status_code == 200 else {}

    default_to = customer.get("email", "")
    title = quote.get("title") or f"Quote SAE-2026-{quote.get('number')}"
    default_subject = f"Estimate SAE-2026-{quote.get('number')} — {title}"
    default_body_html = (
        f'<p>Dear {customer.get("name", "team")},</p>\n'
        f'<p>Please find attached our estimate <b>SAE-2026-{quote.get("number")}</b> '
        f'for <b>{title}</b>.</p>\n'
        f'<p>Total ex GST: ${float(quote.get("subtotal", 0)):,.2f}. '
        f'Valid for {quote.get("validity_days", 28)} days from {quote.get("issue_date", "today")}.</p>\n'
        f'<p>To proceed, please issue a purchase order referencing '
        f'<b>SAE-2026-{quote.get("number")}</b> to admin@saee.com.au. '
        f'Upon receipt of your PO we will issue a tax invoice for the deposit '
        f'amount and schedule works accordingly.</p>\n'
        f'<p>If you have any questions please reply to this email or call '
        f'0457 704 373.</p>\n'
        f'<p>Kind regards,<br>Richard Sauer<br>Director — SAE Engineering</p>'
    )

    return _TEMPLATES.TemplateResponse(
        request, "quotes/email_compose.html",
        {
            "quote":            quote,
            "form":             {},
            "from_options":     _FROM_OPTIONS,
            "default_from":     _DEFAULT_FROM_BY_DOC_TYPE["quote"],
            "default_to":       default_to,
            "default_subject":  default_subject,
            "default_body_html": default_body_html,
            "flash":            None,
            "flash_kind":       None,
        },
    )


@router.post("/quotes/{quote_id}/email", response_class=HTMLResponse, response_model=None)
async def quote_email_send(
    request: Request, quote_id: str
) -> HTMLResponse | RedirectResponse:
    """Submit the composer — POST to upstream /api/v1/quotes/{id}/send-email.

    Whether it actually sends or gets blocked is decided server-side by the
    two-key kill switch. This route just relays + re-renders the form with
    the result.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form = {k: str(v) for k, v in form_data.items() if k != "csrf_token"}

    to_list  = [s.strip() for s in form.get("to", "").split(",") if s.strip()]
    cc_list  = [s.strip() for s in form.get("cc", "").split(",") if s.strip()]
    bcc_list = [s.strip() for s in form.get("bcc", "").split(",") if s.strip()]

    payload = {
        "from_addr":        form.get("from_addr", ""),
        "to":               to_list,
        "cc":               cc_list,
        "bcc":              bcc_list,
        "subject":          form.get("subject", ""),
        "body_html":        form.get("body_html", ""),
        "sent_by_user_id":  request.session.get("user_id"),
    }

    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/quotes/{quote_id}/send-email", json=payload)
        qresp = await client.get(f"/api/v1/quotes/{quote_id}")
        quote = qresp.json() if qresp.status_code == 200 else {}

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    mode = result.get("mode", "unknown")
    if mode == "blocked":
        flash = (
            f"🛑 <b>BLOCKED</b> — nothing was sent. "
            f"Reason: <code>{result.get('reason', 'unknown')}</code>. "
            f"Outbox copy: <code>{result.get('outbox_path', '?')}</code>. "
            f"Audit log id: <code>{result.get('log_id', '?')}</code>."
        )
        flash_kind = "warm"
    elif mode == "sent":
        flash = (
            f"✅ Sent. Resend message id: <code>{result.get('message_id', '?')}</code>. "
            f"Audit log id: <code>{result.get('log_id', '?')}</code>."
        )
        flash_kind = "pos"
    elif mode == "failed":
        flash = (
            f"❌ Failed. Errors: <code>{result.get('errors', '?')}</code>. "
            f"Audit log id: <code>{result.get('log_id', '?')}</code>."
        )
        flash_kind = "neg"
    else:
        flash = f"Upstream HTTP {resp.status_code}: <code>{(resp.text or '')[:300]}</code>"
        flash_kind = "neg"

    return _TEMPLATES.TemplateResponse(
        request, "quotes/email_compose.html",
        {
            "quote":            quote,
            "form":             form,
            "from_options":     _FROM_OPTIONS,
            "default_from":     _DEFAULT_FROM_BY_DOC_TYPE["quote"],
            "default_to":       "",
            "default_subject":  "",
            "default_body_html": "",
            "flash":            flash,
            "flash_kind":       flash_kind,
        },
    )


@router.get("/quotes/_add_line", response_class=HTMLResponse, response_model=None)
async def quote_add_line(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank line row for the given index."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        _, accounts, tax_codes = await _fetch_quote_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "quotes/_line_row.html",
        {
            "index": index,
            "line": {},
            "accounts": accounts,
            "tax_codes": tax_codes,
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Edit — GET (pre-populated form) + POST (PATCH with If-Match + lines replace)
# NOTE: these routes MUST appear before /quotes/{quote_id} for the same
# literal-vs-parameter ordering reason as /quotes/new.
# ---------------------------------------------------------------------------

_EDIT_FIELDS = ("customer_id", "issue_date", "expiry_date", "title", "notes", "terms")
_LOCKED_STATUSES = {"ACCEPTED", "DECLINED", "ARCHIVED", "INVOICED"}


@router.get("/quotes/{quote_id}/edit", response_class=HTMLResponse, response_model=None)
async def quote_edit_form(
    request: Request,
    quote_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing quote."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/quotes/{quote_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "quotes/edit.html",
            {"quote": None, "form": {}, "errors": {"__all__": "Quote not found"},
             "conflict": False, "contacts": [], "accounts": [], "tax_codes": [],
             "lines": [], "line_count": 0},
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "quotes/edit.html",
            {"quote": None, "form": {}, "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
             "conflict": False, "contacts": [], "accounts": [], "tax_codes": [],
             "lines": [], "line_count": 0},
            status_code=resp.status_code,
        )

    quote = resp.json()

    if quote.get("status") in _LOCKED_STATUSES:
        request.session["flash"] = (
            f"Quote is {quote['status'].lower()} and cannot be edited."
        )
        return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)

    form: dict[str, object] = {field: quote.get(field) or "" for field in _EDIT_FIELDS}
    form["version"] = str(quote.get("version", ""))
    form["deposit_pct"] = str(quote.get("deposit_pct", "50"))
    form["late_fee_pct_per_month"] = str(quote.get("late_fee_pct_per_month", "2.5"))
    form["is_supply_only"] = quote.get("is_supply_only", False)

    api_lines = quote.get("lines", [])
    lines = []
    for i, ln in enumerate(api_lines):
        lines.append({
            "index": i,
            "account_id": str(ln.get("account_id") or ""),
            "description": ln.get("description", ""),
            "quantity": str(ln.get("quantity", "1")),
            "unit_price": str(ln.get("unit_price", "")),
            "tax_code_id": str(ln.get("tax_code_id") or ""),
        })
    if not lines:
        lines = [{"index": 0}]

    async with api_client(request) as client:
        contacts, accounts, tax_codes = await _fetch_quote_dropdowns(client)

    return _TEMPLATES.TemplateResponse(
        request,
        "quotes/edit.html",
        {
            "quote": quote,
            "form": form,
            "errors": {},
            "conflict": False,
            "idempotency_key": str(uuid.uuid4()),
            "contacts": contacts,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
        },
    )


@router.post("/quotes/{quote_id}/edit", response_class=HTMLResponse, response_model=None)
async def quote_update(
    request: Request,
    quote_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full lines replace."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")
    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    for field in ("deposit_pct", "late_fee_pct_per_month"):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["is_supply_only"] = bool(form.get("is_supply_only"))
    payload["lines"] = _parse_lines(form)

    from saebooks_web.features import is_feature_enabled as _ff
    _params = {"force": "true"} if _ff("edit_frozen_state") else None
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/quotes/{quote_id}",
            json=payload,
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
            params=_params,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)

    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/quotes/{quote_id}")
            server_quote: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_quote.get("version", ""))
            contacts, accounts, tax_codes = await _fetch_quote_dropdowns(client)

        conflict_form = dict(form)
        conflict_form["version"] = server_version

        raw_lines = _parse_lines(form)
        lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]

        return _TEMPLATES.TemplateResponse(
            request,
            "quotes/edit.html",
            {
                "quote": server_quote,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_quote": server_quote,
                "idempotency_key": idempotency_key,
                "contacts": contacts,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "lines": lines,
                "line_count": len(lines),
            },
            status_code=409,
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
    elif resp.status_code == 428:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "PATCH /api/v1/quotes/%s returned 428 — If-Match header was missing",
            quote_id,
        )
        errors["__all__"] = "Precondition required: version information was missing. Please reload and try again."
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    async with api_client(request) as client:
        contacts2, accounts2, tax_codes2 = await _fetch_quote_dropdowns(client)

    raw_lines2 = _parse_lines(form)
    lines2 = [{"index": i, **ln} for i, ln in enumerate(raw_lines2)] or [{"index": 0}]

    async with api_client(request) as client:
        qresp = await client.get(f"/api/v1/quotes/{quote_id}")
    quote_obj = qresp.json() if qresp.is_success else None

    return _TEMPLATES.TemplateResponse(
        request,
        "quotes/edit.html",
        {
            "quote": quote_obj,
            "form": form,
            "errors": errors,
            "conflict": False,
            "idempotency_key": idempotency_key,
            "contacts": contacts2,
            "accounts": accounts2,
            "tax_codes": tax_codes2,
            "lines": lines2,
            "line_count": len(lines2),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


async def _transition(
    request: Request,
    quote_id: str,
    api_action: str,
    flash_ok: str,
    flash_conflict: str = "Version conflict — try again.",
) -> RedirectResponse:
    """POST a state transition to the API; redirect to detail with flash."""
    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/quotes/{quote_id}/{api_action}",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = flash_ok
        return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = flash_conflict
        return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)

    # 422 or other
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.post("/quotes/{quote_id}/send", response_class=HTMLResponse, response_model=None)
async def quote_send(request: Request, quote_id: str) -> RedirectResponse:
    """DRAFT → SENT."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    return await _transition(request, quote_id, "send", "Quote sent.")


@router.post("/quotes/{quote_id}/accept", response_class=HTMLResponse, response_model=None)
async def quote_accept(request: Request, quote_id: str) -> RedirectResponse:
    """SENT → ACCEPTED."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    return await _transition(request, quote_id, "accept", "Quote accepted.")


@router.post("/quotes/{quote_id}/decline", response_class=HTMLResponse, response_model=None)
async def quote_decline(request: Request, quote_id: str) -> RedirectResponse:
    """SENT → DECLINED."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    return await _transition(request, quote_id, "decline", "Quote declined.")


@router.post("/quotes/{quote_id}/archive", response_class=HTMLResponse, response_model=None)
async def quote_archive(request: Request, quote_id: str) -> RedirectResponse:
    """Any non-INVOICED → ARCHIVED."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    return await _transition(request, quote_id, "archive", "Quote archived.")


@router.post(
    "/quotes/{quote_id}/convert-to-invoice",
    response_class=HTMLResponse,
    response_model=None,
)
async def quote_convert_to_invoice(
    request: Request, quote_id: str
) -> RedirectResponse:
    """ACCEPTED → INVOICED — redirect to the created invoice on success."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/quotes/{quote_id}/convert-to-invoice",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        body = resp.json()
        invoice_id = body.get("invoice_id")
        request.session["flash"] = "Quote converted to invoice."
        if invoice_id:
            return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)
        return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)

    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail — GET /quotes/{quote_id}
# NOTE: must appear AFTER all literal sub-paths (/new, /_add_line, /{id}/edit,
# /{id}/send, /{id}/accept, /{id}/decline, /{id}/archive,
# /{id}/convert-to-invoice).
# ---------------------------------------------------------------------------


@router.get("/quotes/{quote_id}", response_class=HTMLResponse, response_model=None)
async def quote_detail(
    request: Request,
    quote_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single quote detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/quotes/{quote_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "quotes/detail.html",
                {"quote": None, "error": "Quote not found", "flash": None,
                 "email_log": []},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "quotes/detail.html",
                {"quote": None, "error": f"API error: HTTP {resp.status_code}",
                 "flash": None, "email_log": []},
                status_code=resp.status_code,
            )
        # Best-effort: pull send history for this quote — don't fail the page
        # if the email-log endpoint is unavailable.
        email_log: list = []
        try:
            log_resp = await client.get(f"/api/v1/email-log/by-doc/quote/{quote_id}")
            if log_resp.status_code == 200:
                email_log = log_resp.json().get("items", [])
        except Exception:
            email_log = []

    quote = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "quotes/detail.html",
        {
            "quote":     quote,
            "error":     None,
            "flash":     flash,
            "email_log": email_log,
        },
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /quotes/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_QUOTES = {
    "send": ("POST", "/api/v1/quotes/{id}/send"),
    "accept": ("POST", "/api/v1/quotes/{id}/accept"),
    "decline": ("POST", "/api/v1/quotes/{id}/decline"),
    "archive": ("POST", "/api/v1/quotes/{id}/archive"),
}


@router.post("/quotes/bulk", response_class=HTMLResponse, response_model=None)
async def quotes_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many quotes at once.

    Form fields:
      action  — one of: send, accept, decline, archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /quotes. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_QUOTES:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/quotes", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/quotes", status_code=303)

    method, path_tpl = _BULK_ACTIONS_QUOTES[action]
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
        request.session["flash"] = f"{label}: {ok} quote{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/quotes", status_code=303)
