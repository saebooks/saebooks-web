"""Tests for the payment edit form — Lane D cycle 19.

Eight tests:
1. test_payment_edit_requires_auth
       GET /payments/{id}/edit without session -> 303 /login
2. test_payment_edit_form_renders_draft_with_allocations
       Mock DRAFT payment with invoice allocation -> form with version hidden input +
       allocation rows, target_type correctly synthesised from invoice_id.
3. test_payment_edit_blocked_for_posted
       Mock POSTED payment -> blocked page (422), no form.
4. test_payment_edit_blocked_for_voided
       Mock VOIDED payment -> blocked page (422), no form.
5. test_payment_edit_success_redirects
       POST with valid body; mock PATCH 200 -> 303 to /payments/{id}.
6. test_payment_edit_conflict_shows_banner
       Mock PATCH 409 + re-GET -> amber banner + new version + user input preserved.
7. test_payment_edit_validation_error_rerenders
       Mock PATCH 422 structured detail -> form re-renders with field error.
8. test_payment_edit_allocation_sum_mismatch_shows_all_banner
       Mock PATCH 422 plain-string detail (allocation sum mismatch) ->
       form re-renders with __all__ banner.
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

_MOCK_PAYMENT_DRAFT = {
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
    "version": 2,
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

_MOCK_PAYMENT_POSTED = {**_MOCK_PAYMENT_DRAFT, "status": "POSTED", "version": 3}
_MOCK_PAYMENT_VOIDED = {**_MOCK_PAYMENT_DRAFT, "status": "VOIDED", "version": 3}

# A newer server version returned after a 409 conflict.
_MOCK_PAYMENT_V3 = {
    **_MOCK_PAYMENT_DRAFT,
    "version": 3,
    "reference": "UPDATED-BY-SOMEONE-ELSE",
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
# 1. Edit requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_payment_edit_requires_auth() -> None:
    """GET /payments/{id}/edit without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}/edit")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Edit form renders for DRAFT payment with existing allocations
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_form_renders_draft_with_allocations(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /payments/{id}/edit for a DRAFT payment renders the edit form.

    Checks:
    - version hidden input present with the correct value
    - existing allocation row pre-populated with target_type=INVOICE + correct UUID
    - payment_date and method fields present and pre-filled
    - idempotency key input present
    """
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_DRAFT)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}/edit")

    assert resp.status_code == 200
    # Version hidden input with correct value.
    assert 'name="version"' in resp.text
    assert 'value="2"' in resp.text
    # Idempotency key input.
    assert 'name="idempotency_key"' in resp.text
    # Header fields pre-filled.
    assert 'name="payment_date"' in resp.text
    assert "2026-04-23" in resp.text
    assert 'name="method"' in resp.text
    # Reference field present and pre-filled.
    assert 'name="reference"' in resp.text
    assert "TXN-001" in resp.text
    # Existing allocation row at index 0 — target_type synthesised from invoice_id.
    assert 'name="allocations[0][target_type]"' in resp.text
    assert 'name="allocations[0][target_id]"' in resp.text
    assert _INVOICE_ID in resp.text
    # INVOICE option must be selected for this allocation.
    assert "INVOICE" in resp.text


# ---------------------------------------------------------------------------
# 3. Edit blocked for POSTED payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_blocked_for_posted(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /payments/{id}/edit for a POSTED payment shows the blocked page (422)."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}/edit")

    assert resp.status_code == 422
    # Must NOT render the edit form.
    assert 'name="version"' not in resp.text
    assert 'name="payment_date"' not in resp.text
    # Must show the blocked message.
    assert "cannot be edited" in resp.text


# ---------------------------------------------------------------------------
# 4. Edit blocked for VOIDED payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_blocked_for_voided(
    respx_mock: respx.MockRouter,
) -> None:
    """GET /payments/{id}/edit for a VOIDED payment shows the blocked page (422)."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_VOIDED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}/edit")

    assert resp.status_code == 422
    # Must NOT render the edit form.
    assert 'name="version"' not in resp.text
    # Must show the blocked message.
    assert "cannot be edited" in resp.text


# ---------------------------------------------------------------------------
# 5. POST happy path — 200 from API -> 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_success_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /payments/{id}/edit with valid body; API 200 -> 303 to detail page."""
    respx_mock.patch(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_DRAFT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/edit",
            data={
                "payment_date": "2026-04-23",
                "method": "eft",
                "reference": "TXN-001",
                "bank_account_id": _BANK_ACCOUNT_ID,
                "version": "2",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "allocations[0][target_type]": "INVOICE",
                "allocations[0][target_id]": _INVOICE_ID,
                "allocations[0][amount]": "500.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"


# ---------------------------------------------------------------------------
# 6. POST 409 conflict — banner shown + version refreshed + input preserved
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_conflict_shows_banner(
    respx_mock: respx.MockRouter,
) -> None:
    """POST with stale version; API 409 -> re-render form with conflict banner + new version."""
    respx_mock.patch(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(409, json={"detail": "version mismatch"})
    )
    # The route re-fetches the payment after 409 to get the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_V3)
    )
    _mock_dropdowns(respx_mock)

    _my_reference = "MY-UPDATED-REF"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/edit",
            data={
                "payment_date": "2026-04-23",
                "method": "eft",
                "reference": _my_reference,
                "version": "2",  # stale — server is at 3
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "allocations[0][target_type]": "INVOICE",
                "allocations[0][target_id]": _INVOICE_ID,
                "allocations[0][amount]": "500.00",
            },
        )

    assert resp.status_code == 409
    # Conflict banner visible.
    assert "conflict-banner" in resp.text
    assert "Someone else updated this payment" in resp.text
    # Hidden version input updated to the server's latest version (3).
    assert 'value="3"' in resp.text
    # User's submitted reference preserved.
    assert _my_reference in resp.text


# ---------------------------------------------------------------------------
# 7. POST 422 structured validation error re-renders form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_validation_error_rerenders(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /payments/{id}/edit where API returns 422 structured detail re-renders form."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "payment_date"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.patch(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/edit",
            data={
                "method": "eft",
                "reference": "TXN-NODATE",
                "version": "2",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "allocations[0][target_type]": "INVOICE",
                "allocations[0][target_id]": _INVOICE_ID,
                "allocations[0][amount]": "500.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — method field still present.
    assert 'name="method"' in resp.text
    # Field error visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 8. POST 422 plain-string detail (allocation-sum mismatch) -> __all__ banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_edit_allocation_sum_mismatch_shows_all_banner(
    respx_mock: respx.MockRouter,
) -> None:
    """API 422 with plain-string detail (allocation exceeds payment amount) causes
    form re-render with the error message in the __all__ banner."""
    _error_msg = "Total allocated (600.00) exceeds payment amount (500.00)"
    respx_mock.patch(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(422, json={"detail": _error_msg})
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/edit",
            data={
                "payment_date": "2026-04-23",
                "method": "eft",
                "reference": "TXN-001",
                "version": "2",
                "idempotency_key": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "allocations[0][target_type]": "INVOICE",
                "allocations[0][target_id]": _INVOICE_ID,
                "allocations[0][amount]": "600.00",  # exceeds payment amount
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — method field still present.
    assert 'name="method"' in resp.text
    # API plain-string error appears in the __all__ banner.
    assert _error_msg in resp.text
