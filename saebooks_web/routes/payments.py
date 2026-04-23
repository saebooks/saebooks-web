"""Payments list, detail, and create views — Lane D cycles 4 + 18.

GET /payments
    Renders templates/payments/list.html (full page) or
    templates/payments/_table.html (HTMX fragment when HX-Request header present).
    Query params: direction, contact_id, date_from, date_to, limit (default 50), offset.
    Calls GET /api/v1/payments with matching params.

GET /payments/new
    Empty create form with one starter allocation row.
    Fetches all contacts (CUSTOMER + SUPPLIER), bank accounts, for dropdowns.
    Generates a fresh idempotency key to prevent double-submit.

POST /payments/new
    Parse form fields + allocations[N][field] rows, POST to /api/v1/payments.
    X-Idempotency-Key forwarded from hidden input.
    201 → 303 redirect to /payments/{id}.
    422 → re-render with errors + preserved values.

GET /payments/_add_allocation
    HTMX partial: returns a single blank <tr> allocation row for the given index.

GET /payments/{id}
    Renders templates/payments/detail.html.
    Calls GET /api/v1/payments/{id}.

Route ordering: /new and /_add_allocation MUST be declared BEFORE /{payment_id}
so FastAPI resolves the literal paths first.

Allocation schema (PaymentAllocationCreate):
    invoice_id      : UUID | None
    bill_id         : UUID | None
    credit_note_id  : UUID | None
    amount          : Decimal

Direction enums (PaymentDirection):
    INCOMING — customer receipt (AR direction; expected contact_type CUSTOMER)
    OUTGOING — supplier payment (AP direction; expected contact_type SUPPLIER)

Method enums (PaymentMethod, lowercase values):
    cash | eft | cheque | card | direct_deposit | other

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

# Payment method choices for the form — (value, label) pairs.
_METHOD_CHOICES = [
    ("eft", "EFT / Bank transfer"),
    ("cash", "Cash"),
    ("cheque", "Cheque"),
    ("card", "Card"),
    ("direct_deposit", "Direct deposit"),
    ("other", "Other"),
]

# Allocation row fields (used by the parser).
_ALLOC_FIELDS = ("target_type", "target_id", "amount")


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _parse_allocations(form: dict[str, str]) -> list[dict[str, object]]:
    """Extract allocation dicts from a flat form dict.

    Convention: fields are named ``allocations[N][field]`` where N is a
    zero-based integer index.  Fields:
        target_type : "INVOICE" | "BILL"
        target_id   : UUID string
        amount      : Decimal-compatible string

    Rows where target_id is blank are skipped (avoids phantom blank rows).
    The returned dicts use ``invoice_id`` / ``bill_id`` keys that match
    PaymentAllocationCreate in the API schema.
    """
    indices: set[int] = set()
    for key in form:
        if key.startswith("allocations[") and "][" in key:
            try:
                idx = int(key.split("[")[1].split("]")[0])
                indices.add(idx)
            except (ValueError, IndexError):
                pass

    allocations: list[dict[str, object]] = []
    for idx in sorted(indices):
        target_type = form.get(f"allocations[{idx}][target_type]", "").strip()
        target_id = form.get(f"allocations[{idx}][target_id]", "").strip()
        amount_raw = form.get(f"allocations[{idx}][amount]", "").strip()

        # Skip entirely blank rows.
        if not target_id:
            continue

        alloc: dict[str, object] = {"amount": amount_raw or "0"}
        if target_type == "INVOICE":
            alloc["invoice_id"] = target_id
        elif target_type == "BILL":
            alloc["bill_id"] = target_id
        else:
            # Default: treat as invoice if type is missing.
            alloc["invoice_id"] = target_id

        allocations.append(alloc)
    return allocations


async def _fetch_payment_dropdowns(
    client,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch customers, suppliers, and bank accounts for the payment form."""
    customers: list[dict] = []
    suppliers: list[dict] = []
    bank_accounts: list[dict] = []

    c_resp = await client.get(
        "/api/v1/contacts",
        params={"contact_type": "CUSTOMER", "limit": 200, "offset": 0},
    )
    if c_resp.is_success:
        customers = c_resp.json().get("items", [])

    s_resp = await client.get(
        "/api/v1/contacts",
        params={"contact_type": "SUPPLIER", "limit": 200, "offset": 0},
    )
    if s_resp.is_success:
        suppliers = s_resp.json().get("items", [])

    # Bank accounts: filter to ASSET type accounts — no dedicated endpoint,
    # so we fetch all accounts and rely on the template to show a useful list.
    a_resp = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if a_resp.is_success:
        bank_accounts = a_resp.json().get("items", [])

    return customers, suppliers, bank_accounts


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/payments", response_class=HTMLResponse, response_model=None)
async def payments_list(
    request: Request,
    direction: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the payments list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``payments/_table.html`` partial only.  Otherwise the full page
    (``payments/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # The API uses page/page_size rather than limit/offset.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if direction:
        params["direction"] = direction
    if contact_id:
        params["contact_id"] = contact_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    error: str | None = None
    payments: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/payments", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            payments = payload.get("items", [])
            total = payload.get("total", len(payments))
        else:
            error = f"API error: HTTP {resp.status_code}"

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    ctx = {
        "payments": payments,
        "total": total,
        "error": error,
        # Filter values echoed back to the form.
        "filter_direction": direction or "",
        "filter_contact_id": contact_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    # HTMX requests get just the table fragment.
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "payments/_table.html" if is_htmx else "payments/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Create — GET (empty form) + POST (submit)
# NOTE: /payments/new and /payments/_add_allocation MUST appear BEFORE
# /payments/{payment_id} so FastAPI resolves the literal paths first.
# ---------------------------------------------------------------------------


@router.get("/payments/new", response_class=HTMLResponse, response_model=None)
async def payment_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the empty create-payment form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates contact and bank-account dropdowns
    from the upstream API.  One blank allocation row is provided as a starter.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today().isoformat()

    async with api_client(request) as client:
        customers, suppliers, bank_accounts = await _fetch_payment_dropdowns(client)

    initial_allocations = [{"index": 0}]

    return _TEMPLATES.TemplateResponse(
        request,
        "payments/new.html",
        {
            "form": {
                "payment_date": today,
                "direction": "INCOMING",
                "method": "eft",
            },
            "errors": {},
            "idempotency_key": str(uuid.uuid4()),
            "customers": customers,
            "suppliers": suppliers,
            "bank_accounts": bank_accounts,
            "method_choices": _METHOD_CHOICES,
            "allocations": initial_allocations,
            "allocation_count": 1,
        },
    )


@router.post("/payments/new", response_class=HTMLResponse, response_model=None)
async def payment_create(request: Request) -> HTMLResponse | RedirectResponse:
    """Submit the create-payment form.

    Calls POST /api/v1/payments on the upstream API.
    - 201 -> 303 redirect to /payments/{id}  (Post-Redirect-Get)
    - 422 -> re-render form with per-field errors + submitted values preserved
    - 401 -> clear session, redirect to /login
    - other errors -> re-render form with a generic error message

    Allocation fields follow the ``allocations[N][field]`` naming convention
    parsed by ``_parse_allocations()``.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the top-level payload.
    payload: dict[str, object] = {}
    for field in (
        "contact_id",
        "payment_date",
        "amount",
        "direction",
        "method",
        "reference",
        "notes",
        "bank_account_id",
    ):
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    payload["allocations"] = _parse_allocations(form)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/payments",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        return RedirectResponse(url=f"/payments/{created['id']}", status_code=303)

    # Parse errors for re-render.
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
                # API returns a plain string for allocation/amount violations.
                errors["__all__"] = detail
        except Exception:
            errors["__all__"] = f"Validation error (HTTP {resp.status_code})"
    else:
        errors["__all__"] = f"API error: HTTP {resp.status_code}"

    # Re-fetch dropdowns for re-render.
    async with api_client(request) as client:
        customers, suppliers, bank_accounts = await _fetch_payment_dropdowns(client)

    # Reconstruct allocation rows for re-render from submitted form keys.
    raw_allocs = _parse_allocations(form)
    alloc_rows = (
        [{"index": i, **a} for i, a in enumerate(raw_allocs)]
        if raw_allocs
        else [{"index": 0}]
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "payments/new.html",
        {
            "form": form,
            "errors": errors,
            "idempotency_key": idempotency_key,
            "customers": customers,
            "suppliers": suppliers,
            "bank_accounts": bank_accounts,
            "method_choices": _METHOD_CHOICES,
            "allocations": alloc_rows,
            "allocation_count": len(alloc_rows),
        },
        status_code=422 if resp.status_code == 422 else resp.status_code,
    )


@router.get(
    "/payments/_add_allocation", response_class=HTMLResponse, response_model=None
)
async def payment_add_allocation(
    request: Request, index: int = 0
) -> HTMLResponse | RedirectResponse:
    """HTMX partial: return a single blank allocation row for the given index.

    Called via hx-get="/payments/_add_allocation?index=N" to append a new row
    to the allocations table without a full page reload.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "payments/_allocation_row.html",
        {
            "index": index,
            "alloc": {},
            "errors": {},
        },
    )


# ---------------------------------------------------------------------------
# Detail
# NOTE: /{payment_id} MUST appear AFTER /new and /_add_allocation.
# ---------------------------------------------------------------------------


@router.get("/payments/{payment_id}", response_class=HTMLResponse, response_model=None)
async def payment_detail(
    request: Request,
    payment_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single payment detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/payments/{payment_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "payments/detail.html",
                {"payment": None, "error": "Payment not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "payments/detail.html",
                {"payment": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    payment = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "payments/detail.html",
        {"payment": payment, "error": None},
    )
