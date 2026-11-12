"""Tests for the payment create form — Lane D cycle 18.

Seven tests:
1. test_payment_new_form_renders_with_direction_default
       GET /payments/new returns 200 with INCOMING direction pre-selected
       and one starter allocation row.
2. test_payment_add_allocation_fragment
       GET /payments/_add_allocation?index=2 returns a <tr> fragment (no <html>).
3. test_payment_create_incoming_invoice_redirects
       POST with direction=INCOMING + single invoice allocation; mock API 201;
       expect 303 to /payments/{id}.
4. test_payment_create_outgoing_bill_redirects
       POST with direction=OUTGOING + single bill allocation; mock API 201;
       expect 303 to /payments/{id}.
5. test_payment_create_422_allocation_mismatch_renders_banner
       API returns 422 with plain-string detail; form re-renders and shows
       the error message in the __all__ banner.
6. test_payment_create_validation_error_preserves_input
       API returns 422 with structured detail list; form re-renders and
       submitted values are preserved.
7. test_payment_create_two_allocation_rows_after_htmx_round_trip
       GET _add_allocation twice (index=0, index=1) — both fragments contain
       the correct per-index field names.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

# ---------------------------------------------------------------------------
# Constants / mock data
# ---------------------------------------------------------------------------

_PAYMENT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTACT_CUSTOMER_ID = "11111111-1111-1111-1111-111111111111"
_CONTACT_SUPPLIER_ID = "22222222-2222-2222-2222-222222222222"
_INVOICE_ID = "33333333-3333-3333-3333-333333333333"
_BILL_ID = "44444444-4444-4444-4444-444444444444"
_BANK_ACCOUNT_ID = "55555555-5555-5555-5555-555555555555"

_MOCK_CUSTOMER = {
    "id": _CONTACT_CUSTOMER_ID,
    "name": "Acme Corp",
    "contact_type": "CUSTOMER",
    "email": None,
    "phone": None,
    "abn": None,
    "address_line1": None,
    "address_line2": None,
    "city": None,
    "state": None,
    "postcode": None,
    "country": "Australia",
    "notes": None,
    "default_account_id": None,
    "default_tax_code": None,
    "bank_bsb": None,
    "bank_account_number": None,
    "bank_account_title": None,
    "company_id": "66666666-6666-6666-6666-666666666666",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 1,
    "archived_at": None,
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-04-01T00:00:00Z",
}

_MOCK_SUPPLIER = {
    **_MOCK_CUSTOMER,
    "id": _CONTACT_SUPPLIER_ID,
    "name": "Tools Pty Ltd",
    "contact_type": "SUPPLIER",
}

_MOCK_CUSTOMERS_RESPONSE = {
    "items": [_MOCK_CUSTOMER],
    "total": 1,
    "limit": 200,
    "offset": 0,
}

_MOCK_SUPPLIERS_RESPONSE = {
    "items": [_MOCK_SUPPLIER],
    "total": 1,
    "limit": 200,
    "offset": 0,
}

_MOCK_BANK_ACCOUNT = {
    "id": _BANK_ACCOUNT_ID,
    "name": "Operating Account",
    "code": "1100",
    "account_type": "ASSET",
    "description": None,
    "company_id": "66666666-6666-6666-6666-666666666666",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "archived_at": None,
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-04-01T00:00:00Z",
}

_MOCK_ACCOUNTS_RESPONSE = {
    "items": [_MOCK_BANK_ACCOUNT],
    "total": 1,
    "limit": 200,
    "offset": 0,
}

_MOCK_PAYMENT = {
    "id": _PAYMENT_ID,
    "company_id": "66666666-6666-6666-6666-666666666666",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_CUSTOMER_ID,
    "bank_account_id": _BANK_ACCOUNT_ID,
    "number": "PAY-0001",
    "direction": "INCOMING",
    "method": "eft",
    "status": "DRAFT",
    "payment_date": "2026-04-23",
    "amount": "500.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "base_amount": "500.00",
    "reference": "TXN-001",
    "notes": None,
    "posted_at": None,
    "posted_by": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "allocations": [
        {
            "id": "77777777-7777-7777-7777-777777777777",
            "payment_id": _PAYMENT_ID,
            "invoice_id": _INVOICE_ID,
            "bill_id": None,
            "credit_note_id": None,
            "amount": "500.00",
        }
    ],
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    """Register mock responses for all three dropdown API calls."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        side_effect=lambda req: (
            Response(200, json=_MOCK_CUSTOMERS_RESPONSE)
            if "CUSTOMER" in str(req.url)
            else Response(200, json=_MOCK_SUPPLIERS_RESPONSE)
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )


# ---------------------------------------------------------------------------
# 1. GET /payments/new — form renders with INCOMING default + one starter row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_new_form_renders_with_direction_default(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /payments/new returns 200 with direction=INCOMING pre-selected and
    one allocation starter row (index 0)."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/payments/new")

    assert resp.status_code == 200
    # Required header fields present.
    assert 'name="direction"' in resp.text
    assert 'name="payment_date"' in resp.text
    assert 'name="amount"' in resp.text
    assert 'name="method"' in resp.text
    # INCOMING selected by default.
    assert "INCOMING" in resp.text
    # Idempotency key.
    assert 'name="idempotency_key"' in resp.text
    # One starter allocation row (index 0).
    assert 'name="allocations[0][target_type]"' in resp.text
    assert 'name="allocations[0][target_id]"' in resp.text
    assert 'name="allocations[0][amount]"' in resp.text
    # Contact dropdown populated.
    assert "Acme Corp" in resp.text


# ---------------------------------------------------------------------------
# 2. GET /payments/_add_allocation — HTMX fragment, no full page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_add_allocation_fragment(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /payments/_add_allocation?index=2 returns a <tr> fragment with
    index=2 field names and no <html> wrapper."""
    # _add_allocation does not call any upstream APIs, but respx is still used
    # here for consistency in case any accidental upstream call is made (it
    # would raise, failing the test, which is the desired behaviour).

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/payments/_add_allocation?index=2")

    assert resp.status_code == 200
    # Fragment — no full-page wrapper.
    assert "<html" not in resp.text
    # Must contain correct index in field names.
    assert 'name="allocations[2][target_type]"' in resp.text
    assert 'name="allocations[2][target_id]"' in resp.text
    assert 'name="allocations[2][amount]"' in resp.text


# ---------------------------------------------------------------------------
# 3. POST /payments/new — INCOMING + invoice allocation → 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_create_incoming_invoice_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """POST with direction=INCOMING + one invoice allocation: mock API 201 →
    303 redirect to /payments/{id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/payments/new",
            data={
                "direction": "INCOMING",
                "contact_id": _CONTACT_CUSTOMER_ID,
                "payment_date": "2026-04-23",
                "amount": "500.00",
                "method": "eft",
                "reference": "TXN-001",
                "bank_account_id": _BANK_ACCOUNT_ID,
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "allocations[0][target_type]": "INVOICE",
                "allocations[0][target_id]": _INVOICE_ID,
                "allocations[0][amount]": "500.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"


# ---------------------------------------------------------------------------
# 4. POST /payments/new — OUTGOING + bill allocation → 303 redirect
# ---------------------------------------------------------------------------


_MOCK_PAYMENT_OUT = {
    **_MOCK_PAYMENT,
    "id": "88888888-8888-8888-8888-888888888888",
    "direction": "OUTGOING",
    "contact_id": _CONTACT_SUPPLIER_ID,
    "allocations": [
        {
            "id": "99999999-9999-9999-9999-999999999999",
            "payment_id": "88888888-8888-8888-8888-888888888888",
            "invoice_id": None,
            "bill_id": _BILL_ID,
            "credit_note_id": None,
            "amount": "250.00",
        }
    ],
}


@pytest.mark.anyio
@respx.mock
async def test_payment_create_outgoing_bill_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """POST with direction=OUTGOING + one bill allocation: mock API 201 →
    303 redirect to /payments/{id}."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT_OUT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/payments/new",
            data={
                "direction": "OUTGOING",
                "contact_id": _CONTACT_SUPPLIER_ID,
                "payment_date": "2026-04-23",
                "amount": "250.00",
                "method": "eft",
                "bank_account_id": _BANK_ACCOUNT_ID,
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "allocations[0][target_type]": "BILL",
                "allocations[0][target_id]": _BILL_ID,
                "allocations[0][amount]": "250.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_MOCK_PAYMENT_OUT['id']}"


# ---------------------------------------------------------------------------
# 5. POST /payments/new — 422 plain-string detail renders __all__ banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_create_422_allocation_mismatch_renders_banner(
    respx_mock: respx.MockRouter,
) -> None:
    """API 422 with plain-string detail (e.g. allocation mismatch) causes
    form re-render with the error message in the __all__ banner."""
    _error_msg = "Allocation amounts must be positive"
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(422, json={"detail": _error_msg})
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/payments/new",
            data={
                "direction": "INCOMING",
                "contact_id": _CONTACT_CUSTOMER_ID,
                "payment_date": "2026-04-23",
                "amount": "500.00",
                "method": "eft",
                "bank_account_id": _BANK_ACCOUNT_ID,
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "allocations[0][target_type]": "INVOICE",
                "allocations[0][target_id]": _INVOICE_ID,
                "allocations[0][amount]": "-10.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered.
    assert 'name="payment_date"' in resp.text
    # API error message appears in the banner.
    assert _error_msg in resp.text


# ---------------------------------------------------------------------------
# 6. POST /payments/new — 422 structured errors preserve input
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_create_validation_error_preserves_input(
    respx_mock: respx.MockRouter,
) -> None:
    """POST with a missing required field: API 422 structured detail causes
    form re-render and the submitted values are echoed back."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "contact_id"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_dropdowns(respx_mock)

    _ref = "PRESERVE-ME"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/payments/new",
            data={
                "direction": "INCOMING",
                "payment_date": "2026-04-23",
                "amount": "100.00",
                "method": "eft",
                "reference": _ref,
                "bank_account_id": _BANK_ACCOUNT_ID,
                "idempotency_key": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered.
    assert 'name="payment_date"' in resp.text
    # Validation error message displayed.
    assert "Field required" in resp.text
    # Submitted reference preserved in the re-rendered form.
    assert _ref in resp.text


# ---------------------------------------------------------------------------
# 7. Two allocation rows after successive _add_allocation HTMX round-trips
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_create_two_allocation_rows_after_add_allocation(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /payments/_add_allocation at index=0 and index=1 both return
    fragments with the correct per-index field names (simulating two HTMX
    round-trips that each append one allocation row)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp0 = await client.get("/payments/_add_allocation?index=0")
        resp1 = await client.get("/payments/_add_allocation?index=1")

    # Both fragments render successfully.
    assert resp0.status_code == 200
    assert resp1.status_code == 200

    # Neither is a full page.
    assert "<html" not in resp0.text
    assert "<html" not in resp1.text

    # Row 0 fields.
    assert 'name="allocations[0][target_type]"' in resp0.text
    assert 'name="allocations[0][target_id]"' in resp0.text
    assert 'name="allocations[0][amount]"' in resp0.text

    # Row 1 fields.
    assert 'name="allocations[1][target_type]"' in resp1.text
    assert 'name="allocations[1][target_id]"' in resp1.text
    assert 'name="allocations[1][amount]"' in resp1.text


# ---------------------------------------------------------------------------
# 8-13. Payment-form fix (2026-11-12): bank required, numbers resolve,
#       nothing fails silently.
# ---------------------------------------------------------------------------

_BASE_FORM = {
    "direction": "INCOMING",
    "contact_id": _CONTACT_CUSTOMER_ID,
    "payment_date": "2026-11-12",
    "amount": "500.00",
    "method": "eft",
    "idempotency_key": "ffffffff-ffff-ffff-ffff-ffffffffffff",
}

_MOCK_INVOICES_RESPONSE = {
    "items": [{"id": _INVOICE_ID, "number": "INV-000080"}],
    "total": 1,
    "limit": 500,
    "offset": 0,
}
_MOCK_BILLS_RESPONSE = {
    "items": [{"id": _BILL_ID, "number": "BILL-000327", "supplier_reference": "PF2927"}],
    "total": 1,
    "limit": 500,
    "offset": 0,
}


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    )


@pytest.mark.anyio
@respx.mock
async def test_blank_bank_account_shows_error_and_does_not_call_api(
    respx_mock: respx.MockRouter,
) -> None:
    """Bank account left blank: visible error, no POST to the engine."""
    post = respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT)
    )
    _mock_dropdowns(respx_mock)
    async with _client() as client:
        resp = await client.post("/payments/new", data=dict(_BASE_FORM))
    assert resp.status_code == 422
    assert not post.called
    assert "Choose the bank account or card the money went through." in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoice_number_resolves_to_id(respx_mock: respx.MockRouter) -> None:
    """Typing INV-000080 sends the invoice's UUID; blank row amount takes the
    payment amount."""
    post = respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": []})
    )
    data = dict(_BASE_FORM, bank_account_id=_BANK_ACCOUNT_ID)
    data["allocations[0][target_type]"] = "INVOICE"
    data["allocations[0][target_id]"] = "inv-000080"
    data["allocations[0][amount]"] = ""
    async with _client() as client:
        resp = await client.post("/payments/new", data=data)
    assert resp.status_code == 303
    sent = _json.loads(post.calls.last.request.content)
    assert sent["allocations"] == [{"amount": "500.00", "invoice_id": _INVOICE_ID}]
    assert sent["bank_account_id"] == _BANK_ACCOUNT_ID


@pytest.mark.anyio
@respx.mock
async def test_bill_supplier_reference_resolves_even_with_invoice_type(
    respx_mock: respx.MockRouter,
) -> None:
    """Type left on Invoice, supplier's ref typed: falls through to the bill."""
    post = respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT_OUT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(200, json=_MOCK_BILLS_RESPONSE)
    )
    data = dict(_BASE_FORM, direction="OUTGOING", contact_id=_CONTACT_SUPPLIER_ID,
                bank_account_id=_BANK_ACCOUNT_ID, amount="250.00")
    data["allocations[0][target_type]"] = "INVOICE"
    data["allocations[0][target_id]"] = "PF2927"
    data["allocations[0][amount]"] = "250.00"
    async with _client() as client:
        resp = await client.post("/payments/new", data=data)
    assert resp.status_code == 303
    sent = _json.loads(post.calls.last.request.content)
    assert sent["allocations"] == [{"amount": "250.00", "bill_id": _BILL_ID}]


@pytest.mark.anyio
@respx.mock
async def test_unresolvable_number_shows_row_error(respx_mock: respx.MockRouter) -> None:
    """Unknown number: error shown on the row, typed value kept, no POST."""
    post = respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(200, json=_MOCK_BILLS_RESPONSE)
    )
    _mock_dropdowns(respx_mock)
    data = dict(_BASE_FORM, bank_account_id=_BANK_ACCOUNT_ID)
    data["allocations[0][target_type]"] = "INVOICE"
    data["allocations[0][target_id]"] = "INV-999999"
    data["allocations[0][amount]"] = "500.00"
    async with _client() as client:
        resp = await client.post("/payments/new", data=data)
    assert resp.status_code == 422
    assert not post.called
    assert "No invoice or bill numbered &#39;INV-999999&#39; was found" in resp.text \
        or "No invoice or bill numbered 'INV-999999' was found" in resp.text
    assert 'value="INV-999999"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_idempotency_conflict_message_shown_in_banner(
    respx_mock: respx.MockRouter,
) -> None:
    """422 in the {code, message} shape (no ``detail``) reaches the banner."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(422, json={
            "code": "idempotency_key_conflict",
            "message": "X-Idempotency-Key reused with a different request body",
        })
    )
    _mock_dropdowns(respx_mock)
    data = dict(_BASE_FORM, bank_account_id=_BANK_ACCOUNT_ID)
    async with _client() as client:
        resp = await client.post("/payments/new", data=data)
    assert resp.status_code == 422
    assert "X-Idempotency-Key reused with a different request body" in resp.text
    # A fresh key is issued so the corrected form can be resubmitted.
    assert 'value="ffffffff-ffff-ffff-ffff-ffffffffffff"' not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_unslotted_api_error_key_folds_into_banner(
    respx_mock: respx.MockRouter,
) -> None:
    """An error on a field with no slot (e.g. currency) is shown in the banner."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(422, json={"detail": [
            {"loc": ["body", "currency"], "msg": "String should have at most 3 characters"},
        ]})
    )
    _mock_dropdowns(respx_mock)
    data = dict(_BASE_FORM, bank_account_id=_BANK_ACCOUNT_ID)
    async with _client() as client:
        resp = await client.post("/payments/new", data=data)
    assert resp.status_code == 422
    assert "String should have at most 3 characters" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_bank_dropdown_lists_only_reconcilable_accounts(
    respx_mock: respx.MockRouter,
) -> None:
    """Only reconcilable, non-header accounts are offered; no 'optional' label."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json=_MOCK_CUSTOMERS_RESPONSE)
    )
    accounts = {"items": [
        {"id": "a1", "code": "1-1100", "name": "Bank Accounts", "account_type": "ASSET", "is_header": True, "reconcile": False},
        {"id": "a2", "code": "1-1114", "name": "Westpac Business One", "account_type": "ASSET", "is_header": False, "reconcile": True},
        {"id": "a3", "code": "1-1200", "name": "Trade Debtors", "account_type": "ASSET", "is_header": False, "reconcile": False},
        {"id": "a4", "code": "2-1116", "name": "Visa 7474", "account_type": "LIABILITY", "is_header": False, "reconcile": True},
        {"id": "a5", "code": "6-1000", "name": "Advertising", "account_type": "EXPENSE", "is_header": False, "reconcile": False},
    ], "total": 5}
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=accounts)
    )
    async with _client() as client:
        resp = await client.get("/payments/new")
    assert resp.status_code == 200
    assert "Westpac Business One" in resp.text
    assert "Visa 7474" in resp.text
    assert "Trade Debtors" not in resp.text
    assert "Advertising" not in resp.text
    assert "— none —" not in resp.text
    assert 'id="bank_account_id" name="bank_account_id" required' in resp.text



@pytest.mark.anyio
@respx.mock
async def test_pay_bill_link_direction_out_opens_as_outgoing(
    respx_mock: respx.MockRouter,
) -> None:
    """The nav's "Pay bill" link (direction=OUT) must open a supplier payment,
    not a customer receipt; IN maps to INCOMING."""
    _mock_dropdowns(respx_mock)
    async with _client() as client:
        out = await client.get("/payments/new?direction=OUT")
        inc = await client.get("/payments/new?direction=IN")
    assert out.status_code == 200
    assert '<option value="OUTGOING" selected>' in out.text
    assert '<option value="INCOMING" selected>' not in out.text
    assert '<option value="INCOMING" selected>' in inc.text


@pytest.mark.anyio
@respx.mock
async def test_direction_transfer_redirects_to_transfer_form(
    respx_mock: respx.MockRouter,
) -> None:
    async with _client() as client:
        resp = await client.get("/payments/new?direction=TRANSFER")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/transfers/new"


@pytest.mark.anyio
@respx.mock
async def test_create_normalises_short_direction(respx_mock: respx.MockRouter) -> None:
    """A form posted with direction=OUT is sent to the engine as OUTGOING."""
    post = respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json=_MOCK_PAYMENT_OUT)
    )
    data = dict(_BASE_FORM, direction="OUT", contact_id=_CONTACT_SUPPLIER_ID,
                bank_account_id=_BANK_ACCOUNT_ID)
    async with _client() as client:
        resp = await client.post("/payments/new", data=data)
    assert resp.status_code == 303
    assert _json.loads(post.calls.last.request.content)["direction"] == "OUTGOING"
