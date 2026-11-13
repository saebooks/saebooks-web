"""Payments list, detail, create, and edit views — Lane D cycles 4 + 18 + 19.

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

GET /payments/{id}/edit
    Pre-populated edit form for a DRAFT payment.
    POSTED and VOIDED payments render edit_blocked.html (422).
    Existing allocations are converted from invoice_id/bill_id/credit_note_id
    into target_type + target_id pairs for the form UX.

POST /payments/{id}/edit
    PATCH /api/v1/payments/{id} with If-Match header (optimistic locking).
    200 → 303 redirect to /payments/{id} with session flash.
    409 → conflict banner + refreshed server version; user input preserved.
    422 → re-render with per-field or __all__ errors.

POST /payments/{id}/post
    Transition DRAFT -> POSTED. POSTs to /api/v1/payments/{id}/post with
    If-Match + X-Idempotency-Key. 200 -> 303 with flash; 409/422 -> 303
    back to detail with flash.

POST /payments/{id}/void
    Transition POSTED -> VOIDED. DELETE /api/v1/payments/{id} with
    If-Match (the engine has no /void route). 200 -> 303 with flash; 409/422 -> 303 back to detail with flash.

GET /payments/{id}
    Renders templates/payments/detail.html.
    Calls GET /api/v1/payments/{id}.

Route ordering: /new and /_add_allocation MUST be declared BEFORE /{id}/edit,
/{id}/edit BEFORE /{id}/post, /{id}/post BEFORE /{id}/void, /{id}/void BEFORE
/{id}/archive, and /{id}/archive BEFORE /{payment_id} (catch-all), so FastAPI
resolves the literal paths first.

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
from urllib.parse import urlencode
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web import list_helpers as lh
from saebooks_web.api_client import api_client
from saebooks_web.archive_helpers import archive_entity as _archive_entity
from saebooks_web.routes.bad_debt_recovery import detect_recovery_prompt  # Task 11

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

# PaymentUpdate mutable header fields (status excluded — use post/void endpoints).
# contact_id, amount, direction, currency are technically patchable but we treat them
# as immutable in the edit UI: changing direction or amount on a payment with
# allocations causes side-effects that belong to a void+recreate workflow.
# The edit form exposes only the fields that are safe to change in place.
_EDIT_FIELDS = ("payment_date", "reference", "notes", "method", "bank_account_id")

# Statuses that block editing via the web UI.  Only DRAFT payments are mutable.
# POSTED payments have journal entries attached; VOIDED ones are archived.
# Void and re-record if changes are needed to a non-DRAFT payment.
_LOCKED_STATUSES = {"POSTED", "VOIDED"}


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
    keep_bank_id: str | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch customers, suppliers, and bank accounts for the payment form."""
    customers: list[dict] = []
    suppliers: list[dict] = []
    bank_accounts: list[dict] = []

    customers = []
    for _ctype in ("CUSTOMER", "BOTH"):
        _r = await client.get(
            "/api/v1/contacts",
            params={"type": _ctype, "limit": 200, "offset": 0},
        )
        if _r.is_success:
            customers.extend(_r.json().get("items", []))

    suppliers = []
    for _ctype in ("SUPPLIER", "BOTH"):
        _r = await client.get(
            "/api/v1/contacts",
            params={"type": _ctype, "limit": 200, "offset": 0},
        )
        if _r.is_success:
            suppliers.extend(_r.json().get("items", []))

    # Bank accounts: the engine has no dedicated endpoint, so page through the
    # whole chart and keep the reconcilable (bank / card) postable accounts.
    # The API requires bank_account_id, so this list must contain every
    # account a payment can really go through — never a truncated first page.
    all_accounts: list[dict] = []
    offset = 0
    for _ in range(50):  # hard stop: 50 x 1000 accounts
        a_resp = await client.get(
            "/api/v1/accounts", params={"limit": 1000, "offset": offset}
        )
        if not a_resp.is_success:
            break
        body = a_resp.json()
        items = body.get("items", [])
        all_accounts.extend(items)
        offset += len(items)
        if not items or offset >= int(body.get("total", offset)):
            break
    bank_accounts = _bank_accounts_from(all_accounts, keep_id=keep_bank_id)

    return customers, suppliers, bank_accounts


def _bank_accounts_from(
    accounts: list[dict], keep_id: str | None = None
) -> list[dict]:
    """Filter a chart of accounts down to accounts a payment can go through.

    Primary rule: ``reconcile`` is true and the account is not a header
    (bank accounts and credit cards). Fallback when a company has flagged
    nothing reconcilable: every non-header ASSET account, so the form is
    never left with an empty required dropdown. ``keep_id`` (the account
    already on an edited payment) is always kept so editing never silently
    swaps it.
    """
    postable = [a for a in accounts if not a.get("is_header")]
    banks = [a for a in postable if a.get("reconcile")]
    if not banks:
        banks = [
            a for a in postable
            if str(a.get("account_type", "")).upper() == "ASSET"
        ]
    if keep_id and not any(str(a.get("id")) == keep_id for a in banks):
        banks.extend(a for a in accounts if str(a.get("id")) == keep_id)
    return sorted(banks, key=lambda a: str(a.get("code") or ""))


# Error keys that have their own slot in templates/payments/_form_fields.html.
# Anything else is folded into the "__all__" banner so no error is ever lost.
_SLOTTED_ERROR_KEYS = {
    "__all__", "direction", "contact_id", "payment_date", "amount",
    "method", "reference", "bank_account_id", "notes",
}


def _api_errors(resp) -> dict[str, str]:
    """Turn any API error response into form errors.

    Handles FastAPI ``detail`` lists (allocation errors keyed
    ``allocations.N``), plain-string ``detail``, and the ``{code, message}``
    shape the idempotency layer returns. Never returns an empty dict for a
    failed response.
    """
    errors: dict[str, str] = {}
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, list):
            for err in detail:
                if not isinstance(err, dict):
                    continue
                loc = [p for p in err.get("loc", []) if p != "body"]
                msg = err.get("msg", "Invalid value")
                if len(loc) >= 2 and loc[0] == "allocations" and isinstance(loc[1], int):
                    key = f"allocations.{loc[1]}"
                else:
                    key = str(loc[0]) if loc else "__all__"
                errors[key] = msg
        elif isinstance(detail, str) and detail:
            errors["__all__"] = detail
        elif body.get("message"):
            errors["__all__"] = str(body["message"])
    if not errors:
        errors["__all__"] = f"The payment could not be saved (HTTP {resp.status_code})."
    return errors


def _fold_unslotted(errors: dict[str, str], alloc_indices: set[int]) -> dict[str, str]:
    """Copy every error that has no visible slot into the ``__all__`` banner."""
    extra: list[str] = []
    for key, msg in errors.items():
        if key in _SLOTTED_ERROR_KEYS:
            continue
        if key.startswith("allocations."):
            try:
                if int(key.split(".", 1)[1]) in alloc_indices:
                    continue
            except ValueError:
                pass
        extra.append(f"{key.replace('_', ' ')}: {msg}")
    if extra:
        parts = [errors["__all__"]] if errors.get("__all__") else []
        errors["__all__"] = " · ".join(parts + extra)
    return errors


def _raw_allocation_rows(form: dict[str, str]) -> list[dict[str, str]]:
    """Return the allocation rows exactly as typed (non-blank target only),
    in submitted order, for resolution and for re-rendering."""
    indices: set[int] = set()
    for key in form:
        if key.startswith("allocations[") and "][" in key:
            try:
                indices.add(int(key.split("[")[1].split("]")[0]))
            except (ValueError, IndexError):
                pass
    rows: list[dict[str, str]] = []
    for idx in sorted(indices):
        target_id = form.get(f"allocations[{idx}][target_id]", "").strip()
        if not target_id:
            continue
        rows.append({
            "target_type": form.get(f"allocations[{idx}][target_type]", "").strip().upper()
            or "INVOICE",
            "target_id": target_id,
            "amount": form.get(f"allocations[{idx}][amount]", "").strip(),
        })
    return rows


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


_DIRECTION_ALIASES = {
    "IN": "INCOMING", "INCOMING": "INCOMING", "RECEIPT": "INCOMING", "AR": "INCOMING",
    "OUT": "OUTGOING", "OUTGOING": "OUTGOING", "PAYMENT": "OUTGOING", "AP": "OUTGOING",
}


def _normalise_direction(value: str | None) -> str | None:
    """Map the short forms the nav links use (IN / OUT) to the engine's
    PaymentDirection values. Unknown values are returned unchanged so the
    API can reject them visibly."""
    if not value:
        return None
    return _DIRECTION_ALIASES.get(value.strip().upper(), value.strip().upper())


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit()).lstrip("0")


async def _find_document_id(
    client, kind: str, typed: str, contact_id: str | None
) -> tuple[str | None, str | None]:
    """Look an invoice or bill up by the number the user typed.

    Returns ``(id, None)`` on a unique match, ``(None, reason)`` otherwise.
    Matches the document number case-insensitively (bills also match the
    supplier's own reference); if nothing matches exactly, a unique match on
    the numeric part (``80`` -> ``INV-000080``) is accepted.
    """
    path = "/api/v1/invoices" if kind == "INVOICE" else "/api/v1/bills"
    wanted = typed.strip().lower()
    wanted_digits = _digits(typed)
    exact: list[str] = []
    by_digits: list[str] = []
    page = 1
    while page <= 40:  # hard stop: 40 x 500 documents
        params: dict[str, object] = {"page": page, "page_size": 500}
        if contact_id and _is_uuid(contact_id):
            params["contact_id"] = contact_id
        r = await client.get(path, params=params)
        if not r.is_success:
            return None, f"could not look up {kind.lower()}s (HTTP {r.status_code})"
        body = r.json()
        items = body.get("items", [])
        for doc in items:
            refs = [str(doc.get("number") or "")]
            if kind == "BILL":
                refs.append(str(doc.get("supplier_reference") or ""))
            refs = [x for x in refs if x]
            if any(x.lower() == wanted for x in refs):
                exact.append(str(doc["id"]))
            elif wanted_digits and any(_digits(x) == wanted_digits for x in refs[:1]):
                by_digits.append(str(doc["id"]))
        if not items or page * 500 >= int(body.get("total", 0)):
            break
        page += 1
    matches = exact or by_digits
    if len(set(matches)) == 1:
        return matches[0], None
    if len(set(matches)) > 1:
        return None, f"more than one {kind.lower()} matches '{typed}' — use the full number"
    return None, None


async def _resolve_allocations(
    client,
    rows: list[dict[str, str]],
    contact_id: str | None,
    payment_amount: str | None,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Turn typed allocation rows into API allocations.

    A UUID is passed straight through (the invoice "Record payment" prefill).
    Anything else is treated as an invoice / bill number and resolved. If the
    number is not found under the chosen type, the other type is tried (a
    user who leaves the type on "Invoice" and types BILL-000327 still gets
    the bill). A single row with a blank amount takes the payment amount.
    Errors are keyed ``allocations.N`` (N = row position) for the row slot.
    """
    allocations: list[dict[str, object]] = []
    errors: dict[str, str] = {}
    for i, row in enumerate(rows):
        kind = row["target_type"] if row["target_type"] in ("BILL", "CREDIT_NOTE") else "INVOICE"
        typed = row["target_id"]
        doc_id: str | None = None
        if _is_uuid(typed):
            doc_id = typed
        else:
            if typed.upper().startswith("BILL"):
                kind = "BILL"
            elif typed.upper().startswith("INV") or kind == "CREDIT_NOTE":
                kind = "INVOICE"
            doc_id, reason = await _find_document_id(client, kind, typed, contact_id)
            if doc_id is None and reason is None:
                other = "INVOICE" if kind == "BILL" else "BILL"
                doc_id, reason = await _find_document_id(client, other, typed, contact_id)
                if doc_id is not None:
                    kind = other
            if doc_id is None:
                errors[f"allocations.{i}"] = (
                    reason
                    or f"No invoice or bill numbered '{typed}' was found for this contact."
                )
                continue
        amount = row["amount"]
        if not amount:
            if len(rows) == 1 and payment_amount:
                amount = payment_amount
            else:
                errors[f"allocations.{i}"] = "Enter the amount to allocate to this row."
                continue
        alloc: dict[str, object] = {"amount": amount}
        key = {"BILL": "bill_id", "CREDIT_NOTE": "credit_note_id"}.get(kind, "invoice_id")
        alloc[key] = doc_id
        allocations.append(alloc)
    return allocations, errors


def _alloc_rows_for_render(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    """Allocation rows for re-render: what the user typed, re-indexed 0..N."""
    if not rows:
        return [{"index": 0}]
    return [{"index": i, **row} for i, row in enumerate(rows)]


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
    q: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    export: str | None = None,
    sort: str = "payment_date",
    sort_direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse | Response:
    """Render the payments list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``payments/_table.html`` partial only.  Otherwise the full page
    (``payments/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # Search, sort and paging run in the engine over every row.
    sort = sort if sort in ('number', 'payment_date', 'contact', 'amount', 'reference', 'method', 'status') else 'payment_date'
    sort_direction = "asc" if sort_direction == "asc" else "desc"
    limit = lh.clamp_page_size(limit)
    offset = max(0, offset)

    # The API uses page/page_size rather than limit/offset.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    params.update({"sort": sort, "sort_direction": sort_direction})
    search = lh.search_params(q, amount_min, amount_max)
    params.update(search)
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
    contacts_by_id: dict[str, dict] = {}
    filter_contact_name = ""

    async with api_client(request) as client:
        if lh.wants_csv(export):
            resp = await client.get("/api/v1/payments", params={**params, "format": "csv"})
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                return lh.csv_passthrough(resp, "payments.csv")
            request.session["flash"] = lh.export_error_message(resp)
            return RedirectResponse(url="/payments", status_code=303)
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

        # Names for exactly the contacts on this page (and the filter),
        # whatever their type and however many contacts the books hold.
        contacts_by_id = await lh.fetch_contact_names(
            client, [r.get("contact_id") for r in payments] + [contact_id]
        )
        if contact_id:
            filter_contact_name = str(
                (contacts_by_id.get(contact_id) or {}).get("name") or ""
            )

    # Compute pagination offsets for previous / next links.
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    # Consume and clear any flash message (e.g. from a successful archive).
    flash = request.session.pop("flash", None)

    ctx = {
        "payments": payments,
        "contacts_by_id": contacts_by_id,
        "total": total,
        "error": error,
        "flash": flash,
        # Filter values echoed back to the form.
        "filter_direction": direction or "",
        "filter_contact_id": contact_id or "",
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "filter_contact_name": filter_contact_name,
        "filter_q": search.get("q", ""),
        "filter_amount_min": search.get("amount_min", ""),
        "filter_amount_max": search.get("amount_max", ""),
        "sort": sort,
        "sort_direction": sort_direction,
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
async def payment_new_form(
    request: Request,
    contact_id: str | None = None,
    invoice_id: str | None = None,
    bill_id: str | None = None,
    amount: str | None = None,
    direction: str | None = None,
    reference: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the create-payment form.

    Generates a fresh idempotency key stored in a hidden input to prevent
    double-submit on page reload.  Populates contact and bank-account dropdowns
    from the upstream API.  One blank allocation row is provided as a starter.

    Optional query-string prefill (used by the invoices "Record payment"
    link — see invoices/_table.html and invoices/detail.html — the safe
    replacement for a bulk "mark paid" the engine has no endpoint for):
    ``contact_id``, ``invoice_id`` or ``bill_id``, ``amount``, ``direction``,
    ``reference``. With no query params this renders exactly as before
    (blank form, one empty allocation row).
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # A transfer between own accounts is not a payment — send it to the
    # transfer form (origin=TRANSFER) rather than a customer receipt.
    if (direction or "").strip().upper() == "TRANSFER":
        return RedirectResponse(url="/transfers/new", status_code=303)
    direction = _normalise_direction(direction)

    today = date.today().isoformat()

    async with api_client(request) as client:
        customers, suppliers, bank_accounts = await _fetch_payment_dropdowns(client)

    target_id = invoice_id or bill_id
    if target_id:
        target_type = "BILL" if bill_id else "INVOICE"
        initial_allocations = [{
            "index": 0,
            "target_type": target_type,
            "target_id": target_id,
            "amount": amount or "",
        }]
    else:
        initial_allocations = [{"index": 0}]

    form: dict[str, object] = {
        "payment_date": today,
        "direction": direction or "INCOMING",
        "method": "eft",
    }
    if contact_id:
        form["contact_id"] = contact_id
    if amount:
        form["amount"] = amount
    if reference:
        form["reference"] = reference

    return _TEMPLATES.TemplateResponse(
        request,
        "payments/new.html",
        {
            "form": form,
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

    if payload.get("direction"):
        payload["direction"] = _normalise_direction(str(payload["direction"]))
        form["direction"] = str(payload["direction"])

    rows = _raw_allocation_rows(form)
    errors: dict[str, str] = {}
    if not payload.get("bank_account_id"):
        errors["bank_account_id"] = (
            "Choose the bank account or card the money went through."
        )

    resp = None
    async with api_client(request) as client:
        allocations, alloc_errors = await _resolve_allocations(
            client,
            rows,
            str(payload.get("contact_id") or "") or None,
            str(payload.get("amount") or "") or None,
        )
        errors.update(alloc_errors)
        if not errors:
            payload["allocations"] = allocations
            resp = await client.post(
                "/api/v1/payments",
                json=payload,
                headers={"X-Idempotency-Key": idempotency_key},
            )

    if resp is None:
        return await _render_new_with_errors(
            request, form, errors, rows, str(uuid.uuid4()), 422
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        created = resp.json()
        payment_id = created["id"]
        # Task 11 — bad-debt recovery detection. For an INCOMING (AR)
        # receipt from a payer who has a WRITTEN_OFF invoice, and when the
        # company is in smart_prompt recovery mode, redirect to the
        # recovery prompt instead of the payment detail. We resolve the
        # company's recovery_mode from the active company; any failure here
        # falls through to the normal redirect (detection is best-effort).
        contact_id = str(payload.get("contact_id") or "")
        direction = str(payload.get("direction") or "").upper()
        is_incoming = direction in ("INCOMING", "IN", "AR", "RECEIPT")
        if contact_id and is_incoming:
            try:
                async with api_client(request) as client:
                    c_resp = await client.get(
                        "/api/v1/companies", params={"limit": 1, "offset": 0}
                    )
                    recovery_mode = "smart_prompt"
                    if c_resp.is_success:
                        items = c_resp.json().get("items", [])
                        if items:
                            recovery_mode = items[0].get("recovery_mode") or "smart_prompt"
                    written_off = await detect_recovery_prompt(
                        client, contact_id=contact_id, recovery_mode=recovery_mode
                    )
                if written_off:
                    qs = urlencode({
                        "contact_id": contact_id,
                        "amount": str(payload.get("amount") or ""),
                        "bank_account_id": str(payload.get("bank_account_id") or ""),
                        "payment_id": payment_id,
                        "recovery_date": str(payload.get("payment_date") or ""),
                    })
                    return RedirectResponse(
                        url=f"/bad-debts/recovery/prompt?{qs}", status_code=303
                    )
            except Exception:
                pass  # detection is best-effort; never block the payment
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    # Any other outcome: show every error the API gave, nothing silent.
    # After a 422 nothing was created, so a fresh idempotency key lets the
    # corrected form be resubmitted; on other failures keep the key so a
    # retry can never double-record.
    errors = _api_errors(resp)
    next_key = str(uuid.uuid4()) if resp.status_code == 422 else idempotency_key
    return await _render_new_with_errors(
        request, form, errors, rows, next_key, resp.status_code
    )


async def _render_new_with_errors(
    request: Request,
    form: dict[str, str],
    errors: dict[str, str],
    rows: list[dict[str, str]],
    idempotency_key: str,
    status_code: int,
) -> HTMLResponse:
    """Re-render the create form with errors and the user's typed values."""
    async with api_client(request) as client:
        customers, suppliers, bank_accounts = await _fetch_payment_dropdowns(client)
    alloc_rows = _alloc_rows_for_render(rows)
    errors = _fold_unslotted(errors, set(range(len(alloc_rows))))
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
        status_code=status_code,
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
# Edit — GET (pre-populated form) + POST (PATCH with If-Match + allocations replace)
# NOTE: /{payment_id}/edit MUST appear BEFORE the catch-all /{payment_id} so
# FastAPI resolves the literal sub-path first.
# ---------------------------------------------------------------------------


@router.get(
    "/payments/{payment_id}/edit", response_class=HTMLResponse, response_model=None
)
async def payment_edit_form(
    request: Request,
    payment_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the pre-populated edit form for an existing payment.

    Only DRAFT payments are editable.  POSTED or VOIDED payments get the
    read-only ``edit_blocked.html`` page (HTTP 422).

    The current ``version`` is stored in a hidden input for the subsequent
    POST to include in the ``If-Match`` header (optimistic locking).  A
    fresh idempotency key is generated per GET to guard against double-submit.

    Existing allocations are mapped from the API shape (invoice_id / bill_id /
    credit_note_id columns) into the UX shape (target_type + target_id) so
    the allocation row template can display them correctly.
    """
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
            "payments/edit.html",
            {
                "payment": None,
                "form": {},
                "errors": {"__all__": "Payment not found"},
                "conflict": False,
                "customers": [],
                "suppliers": [],
                "bank_accounts": [],
                "method_choices": _METHOD_CHOICES,
                "allocations": [],
                "allocation_count": 0,
            },
            status_code=404,
        )
    if not resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "payments/edit.html",
            {
                "payment": None,
                "form": {},
                "errors": {"__all__": f"API error: HTTP {resp.status_code}"},
                "conflict": False,
                "customers": [],
                "suppliers": [],
                "bank_accounts": [],
                "method_choices": _METHOD_CHOICES,
                "allocations": [],
                "allocation_count": 0,
            },
            status_code=resp.status_code,
        )

    payment = resp.json()

    # Block editing of non-DRAFT payments.
    if payment.get("status") in _LOCKED_STATUSES:
        return _TEMPLATES.TemplateResponse(
            request,
            "payments/edit_blocked.html",
            {"payment": payment},
            status_code=422,
        )

    # Pre-populate the form dict from the API response.
    form: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        form[field] = payment.get(field) or ""
    form["version"] = str(payment.get("version", ""))

    # Build allocation rows for the form, synthesising target_type + target_id
    # from the API's invoice_id / bill_id / credit_note_id fields.
    api_allocs = payment.get("allocations", [])
    alloc_rows: list[dict] = []
    for i, a in enumerate(api_allocs):
        if a.get("invoice_id"):
            target_type = "INVOICE"
            target_id = str(a["invoice_id"])
        elif a.get("bill_id"):
            target_type = "BILL"
            target_id = str(a["bill_id"])
        elif a.get("credit_note_id"):
            target_type = "CREDIT_NOTE"
            target_id = str(a["credit_note_id"])
        else:
            target_type = "INVOICE"
            target_id = ""
        alloc_rows.append({
            "index": i,
            "target_type": target_type,
            "target_id": target_id,
            "amount": str(a.get("amount", "")),
        })
    if not alloc_rows:
        alloc_rows = [{"index": 0}]

    async with api_client(request) as client:
        customers, suppliers, bank_accounts = await _fetch_payment_dropdowns(
            client, keep_bank_id=str(payment.get("bank_account_id") or "") or None
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "payments/edit.html",
        {
            "payment": payment,
            "form": form,
            "errors": {},
            "conflict": False,
            "idempotency_key": str(uuid.uuid4()),
            "customers": customers,
            "suppliers": suppliers,
            "bank_accounts": bank_accounts,
            "method_choices": _METHOD_CHOICES,
            "allocations": alloc_rows,
            "allocation_count": len(alloc_rows),
        },
    )


@router.post(
    "/payments/{payment_id}/edit", response_class=HTMLResponse, response_model=None
)
async def payment_update(
    request: Request,
    payment_id: str,
) -> HTMLResponse | RedirectResponse:
    """Submit the edit form — PATCH to the API with If-Match + full allocations replace.

    Outcomes:
    - 200 OK       -> 303 redirect to /payments/{id} with session flash
    - 409 Conflict -> re-fetch latest record, re-render with conflict banner
                      and the server's current version in the hidden input.
                      The user's submitted values are preserved.
    - 422          -> re-render with per-field or __all__ validation errors.
                      Plain-string detail (e.g. allocation-sum mismatch) goes to
                      errors["__all__"].
    - 401          -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {k: v for k, v in form_data.items()}  # type: ignore[misc]

    version = form.get("version", "")
    idempotency_key = form.get("idempotency_key", str(uuid.uuid4()))

    # Build the PATCH payload — only include non-empty header fields.
    payload: dict[str, object] = {}
    for field in _EDIT_FIELDS:
        val = form.get(field, "").strip()
        if val:
            payload[field] = val

    # Allocations are always sent (full replace semantics, mirrors JE lines).
    rows = _raw_allocation_rows(form)
    async with api_client(request) as client:
        allocations, alloc_errors = await _resolve_allocations(
            client, rows, form.get("contact_id") or None, None
        )
    if alloc_errors:
        return await _render_edit_with_errors(
            request, form, alloc_errors, rows, idempotency_key, 422,
            keep_bank_id=form.get("bank_account_id") or None,
        )
    payload["allocations"] = allocations

    from saebooks_web.features import is_feature_enabled as _ff
    _params = {"force": "true"} if _ff("edit_frozen_state") else None
    async with api_client(request) as client:
        resp = await client.patch(
            f"/api/v1/payments/{payment_id}",
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
        request.session["flash"] = "Payment updated."
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    if resp.status_code == 403:
        request.session["flash"] = "You do not have permission to edit this payment."
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    # 409 Conflict — re-fetch the server's latest version, preserve user input.
    if resp.status_code == 409:
        async with api_client(request) as client:
            latest_resp = await client.get(f"/api/v1/payments/{payment_id}")
            server_payment: dict = latest_resp.json() if latest_resp.is_success else {}
            server_version = str(server_payment.get("version", ""))

            customers, suppliers, bank_accounts = await _fetch_payment_dropdowns(client)

        # Preserve user's submitted form values but update the hidden version.
        conflict_form = dict(form)
        conflict_form["version"] = server_version

        # Reconstruct allocation rows for re-render from submitted values.
        alloc_rows = _alloc_rows_for_render(rows)

        return _TEMPLATES.TemplateResponse(
            request,
            "payments/edit.html",
            {
                "payment": server_payment,
                "form": conflict_form,
                "errors": {},
                "conflict": True,
                "server_payment": server_payment,
                "idempotency_key": idempotency_key,
                "customers": customers,
                "suppliers": suppliers,
                "bank_accounts": bank_accounts,
                "method_choices": _METHOD_CHOICES,
                "allocations": alloc_rows,
                "allocation_count": len(alloc_rows),
            },
            status_code=409,
        )

    # 422 or other — parse per-field or plain-string validation errors.
    if resp.status_code == 428:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "PATCH /api/v1/payments/%s returned 428 — If-Match header was missing",
            payment_id,
        )
        errors = {"__all__": (
            "Precondition required: version information was missing. "
            "Please reload and try again."
        )}
    else:
        errors = _api_errors(resp)

    return await _render_edit_with_errors(
        request, form, errors, rows, idempotency_key, resp.status_code,
        keep_bank_id=form.get("bank_account_id") or None,
    )


async def _render_edit_with_errors(
    request: Request,
    form: dict[str, str],
    errors: dict[str, str],
    rows: list[dict[str, str]],
    idempotency_key: str,
    status_code: int,
    keep_bank_id: str | None = None,
) -> HTMLResponse:
    """Re-render the edit form with errors and the user's typed values."""
    async with api_client(request) as client:
        customers2, suppliers2, bank_accounts2 = await _fetch_payment_dropdowns(
            client, keep_bank_id=keep_bank_id
        )
    alloc_rows2 = _alloc_rows_for_render(rows)
    errors = _fold_unslotted(errors, set(range(len(alloc_rows2))))
    return _TEMPLATES.TemplateResponse(
        request,
        "payments/edit.html",
        {
            "payment": None,
            "form": form,
            "errors": errors,
            "conflict": False,
            "idempotency_key": idempotency_key,
            "customers": customers2,
            "suppliers": suppliers2,
            "bank_accounts": bank_accounts2,
            "method_choices": _METHOD_CHOICES,
            "allocations": alloc_rows2,
            "allocation_count": len(alloc_rows2),
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Post transition — POST /{payment_id}/post
# NOTE: MUST appear before the catch-all /{payment_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/payments/{payment_id}/post", response_class=HTMLResponse, response_model=None
)
async def payment_post(
    request: Request,
    payment_id: str,
) -> RedirectResponse:
    """Transition a DRAFT payment to POSTED.

    POSTs to POST /api/v1/payments/{id}/post with If-Match + X-Idempotency-Key.
    - 200 -> 303 to detail with flash "Payment posted."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422 -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", ""))
    idempotency_key = str(uuid.uuid4())

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/payments/{payment_id}/post",
            headers={
                "If-Match": version,
                "X-Idempotency-Key": idempotency_key,
            },
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Payment posted."
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = "Version conflict — try again."
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    # 422 or other — surface the API's detail message.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)


# ---------------------------------------------------------------------------
# Void transition — POST /{payment_id}/void
# NOTE: MUST appear before the catch-all /{payment_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/payments/{payment_id}/void", response_class=HTMLResponse, response_model=None
)
async def payment_void(
    request: Request,
    payment_id: str,
) -> RedirectResponse:
    """Transition a POSTED payment to VOIDED.

    DELETE /api/v1/payments/{id} with If-Match (the engine's void).
    - 204 -> 303 to detail with flash "Payment voided."
    - 409 -> 303 back to detail with flash "Version conflict — try again."
    - 422 -> 303 back to detail with the API's error message as flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = str(form_data.get("version", "")).strip()

    # The engine voids a payment with DELETE /api/v1/payments/{id} +
    # If-Match: <version> (204). There is no /void route.
    async with api_client(request) as client:
        if not version:
            cur = await client.get(f"/api/v1/payments/{payment_id}")
            if cur.is_success:
                version = str(cur.json().get("version", ""))
        resp = await client.delete(
            f"/api/v1/payments/{payment_id}",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if 200 <= resp.status_code < 300:
        request.session["flash"] = "Payment voided."
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = (
            "Version conflict — the payment changed since this page loaded. "
            "Reload and try again."
        )
        return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

    # 404 / 422 / 428 / other — show exactly what the API said.
    errors = _api_errors(resp)
    request.session["flash"] = "Void failed: " + " · ".join(errors.values())
    return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)


# ---------------------------------------------------------------------------
# Archive — POST /{payment_id}/archive
# NOTE: MUST appear before the catch-all /{payment_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/payments/{payment_id}/archive", response_class=HTMLResponse, response_model=None
)
async def payment_archive(
    request: Request,
    payment_id: str,
) -> RedirectResponse:
    """Soft-archive a payment via DELETE /api/v1/payments/{id} with If-Match.

    Only DRAFT payments may be archived; the API returns 422 for POSTED/VOIDED.
    On success redirects to /payments with a flash.
    On 409 (version conflict) or 422 (gate failure) redirects back to detail.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    version = form_data.get("version", "")

    return await _archive_entity(
        request=request,
        entity_api_path="/api/v1/payments",
        entity_id=payment_id,
        version=str(version),
        entity_label=f"Payment {payment_id}",
        list_url="/payments",
        detail_url=f"/payments/{payment_id}",
    )


# ---------------------------------------------------------------------------
# Detail
# NOTE: /{payment_id} MUST appear AFTER /new, /_add_allocation, /{id}/edit,
# and /{id}/archive.
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
                {"payment": None, "error": "Payment not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "payments/detail.html",
                {"payment": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    payment = resp.json()

    # Fetch attachments for this payment. 503 means vault is disabled — render
    # the panel in its disabled state rather than raising an error.
    attachments: list[dict] = []
    vault_enabled: bool = True
    async with api_client(request) as client:
        att_resp = await client.get(
            "/api/v1/attachments",
            params={"entity_kind": "payment", "entity_id": payment_id},
        )
    if att_resp.status_code == 503:
        vault_enabled = False
    elif att_resp.is_success:
        attachments = att_resp.json()

    # Consume and clear any flash message from session.
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "payments/detail.html",
        {
            "payment": payment,
            "error": None,
            "flash": flash,
            "attachments": attachments,
            "vault_enabled": vault_enabled,
            "entity_kind": "payment",
            "entity_id": payment_id,
        },
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /payments/bulk
# ---------------------------------------------------------------------------

_BULK_ACTIONS_PAYMENTS = {
    "post": ("POST", "/api/v1/payments/{id}/post"),
    "void": ("DELETE", "/api/v1/payments/{id}"),
    "archive": ("DELETE", "/api/v1/payments/{id}"),
}


@router.post("/payments/bulk", response_class=HTMLResponse, response_model=None)
async def payments_bulk_action(request: Request) -> RedirectResponse:
    """Run an action against many payments at once.

    Form fields:
      action  — one of: post, void, archive
      ids[]   — one entry per UUID

    Aggregates per-row outcomes into a flash message and redirects back
    to /payments. Best-effort: a failed row does not halt the batch.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    action = str(form_data.get("action", "")).strip()
    if action not in _BULK_ACTIONS_PAYMENTS:
        request.session["flash"] = f"Unknown bulk action: {action!r}"
        return RedirectResponse(url="/payments", status_code=303)

    ids = [str(v) for v in form_data.getlist("ids[]") if str(v).strip()]
    if not ids:
        request.session["flash"] = "No rows selected."
        return RedirectResponse(url="/payments", status_code=303)

    method, path_tpl = _BULK_ACTIONS_PAYMENTS[action]
    ok = 0
    failed: list[str] = []
    async with api_client(request) as client:
        for row_id in ids:
            try:
                # Every payment transition needs If-Match: <version>; fetch
                # it per row (post also needs an idempotency key).
                headers: dict[str, str] = {}
                cur = await client.get(f"/api/v1/payments/{row_id}")
                if cur.is_success:
                    headers["If-Match"] = str(cur.json().get("version", ""))
                if action == "post":
                    headers["X-Idempotency-Key"] = str(uuid.uuid4())
                resp = await client.request(
                    method, path_tpl.format(id=row_id), headers=headers
                )
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
                        elif body.get("message"):
                            msg = str(body["message"])
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
        request.session["flash"] = f"{label}: {ok} payment{'s' if ok != 1 else ''} processed."
    return RedirectResponse(url="/payments", status_code=303)

# ---------------------------------------------------------------------------
# Hard-delete: developer-tier only. Client-side gated via the kebab,
# server-side enforced by the API hard_delete_admin_gate.
# ---------------------------------------------------------------------------


@router.post("/payments/{payment_id}/hard-delete", response_class=HTMLResponse, response_model=None)
async def payment_hard_delete(request: Request, payment_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    from saebooks_web.archive_helpers import hard_delete_entity
    return await hard_delete_entity(
        request=request,
        entity_api_path="/api/v1/payments",
        entity_id=payment_id,
        entity_label=f"Payment {payment_id}",
        list_url="/payments",
        detail_url=f"/payments/{payment_id}",
    )
