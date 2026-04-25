"""Tests for the bill create form — Lane D cycles 11 + 51.

Twelve tests:
1.  test_bill_new_requires_auth              — GET /bills/new without session -> 303 /login
2.  test_bill_new_form_renders               — GET /bills/new returns 200 with form + starter line
3.  test_bill_add_line_fragment              — GET /bills/_add_line returns <tr> without <html>
4.  test_bill_create_success_redirects       — POST with 2 lines; mock API 201; expect 303 to /bills/{id}
5.  test_bill_create_validation_error        — API 422; form re-renders with user input preserved
6.  test_bill_create_sends_idempotency_key   — API call received X-Idempotency-Key
7.  test_bill_create_requires_auth           — POST /bills/new without session -> 303 /login
8.  test_bill_add_line_requires_auth         — GET /bills/_add_line without session -> 303 /login
9.  test_bill_create_api_401                 — API returns 401 -> session cleared, redirect /login
10. test_bill_create_api_500                 — API 500 -> form re-rendered with generic error
11. test_bill_create_multi_line              — POST with 3 lines -> 303 to /bills/{id}
12. test_bill_create_api_error_string_detail — API 422 with string detail -> error shown
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

_BILL_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"

_MOCK_SUPPLIER = {"id": _CONTACT_ID, "name": "Acme Supplies Pty Ltd", "contact_type": "SUPPLIER"}
_MOCK_CONTACTS = {"items": [_MOCK_SUPPLIER], "total": 1, "limit": 500, "offset": 0}

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "name": "Office Expenses", "code": "6100", "account_type": "EXPENSE"}
_MOCK_ACCOUNTS = {"items": [_MOCK_ACCOUNT], "total": 1, "limit": 500, "offset": 0}

_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}
_MOCK_TAX_CODES = {"items": [_MOCK_TAX_CODE], "total": 1}

_MOCK_BILL = {
    "id": _BILL_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "BILL-0001",
    "supplier_reference": "SUP-INV-123",
    "issue_date": "2026-04-23",
    "due_date": "2026-05-23",
    "status": "DRAFT",
    "subtotal": "200.00",
    "tax_total": "20.00",
    "total": "220.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "notes": None,
    "posted_at": None,
    "posted_by": None,
    "version": 1,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "line_no": 1,
            "description": "Office supplies",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "2.0",
            "unit_price": "100.00",
            "discount_pct": "0.0",
            "line_subtotal": "200.00",
            "line_tax": "20.00",
            "line_total": "220.00",
            "project_id": None,
            "item_id": None,
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
    """Register mock responses for the three dropdown-populating API calls."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json=_MOCK_CONTACTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )


# ---------------------------------------------------------------------------
# 1. GET /bills/new — requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bill_new_requires_auth() -> None:
    """GET /bills/new without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/bills/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. GET /bills/new — form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_new_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /bills/new returns 200 with the create form and a starter line row."""
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/new")

    assert resp.status_code == 200
    # Required fields present
    assert 'name="contact_id"' in resp.text
    assert 'name="issue_date"' in resp.text
    assert 'name="due_date"' in resp.text
    # Idempotency key hidden input
    assert 'name="idempotency_key"' in resp.text
    # At least one line row rendered
    assert 'name="lines[0][description]"' in resp.text
    # Dropdown options populated
    assert "Acme Supplies Pty Ltd" in resp.text
    assert "6100 — Office Expenses" in resp.text
    assert "GST" in resp.text


# ---------------------------------------------------------------------------
# 3. GET /bills/_add_line — HTMX partial returns a line row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_add_line_fragment(respx_mock: respx.MockRouter) -> None:
    """GET /bills/_add_line?index=1 returns the line-row fragment, not a full page."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=_MOCK_TAX_CODES)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/_add_line?index=1")

    assert resp.status_code == 200
    # Must be a fragment, not a full page.
    assert "<html" not in resp.text
    # Must contain the correct index in field names.
    assert 'name="lines[1][description]"' in resp.text
    # Dropdown options populated.
    assert "6100 — Office Expenses" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /bills/new — success redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /bills/new with 2 lines mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(201, json=_MOCK_BILL)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "2",
                "lines[0][unit_price]": "100.00",
                "lines[1][account_id]": _ACCOUNT_ID,
                "lines[1][description]": "Stationery",
                "lines[1][quantity]": "1",
                "lines[1][unit_price]": "50.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"


# ---------------------------------------------------------------------------
# 5. POST /bills/new — validation error re-renders form with preserved input
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST without contact_id: upstream 422 -> re-render the form with errors."""
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
    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(422, json=_422_body)
    )
    # Dropdown re-population after validation failure also needs mocking.
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered — required fields still present.
    assert 'name="contact_id"' in resp.text
    # Error message should appear.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 6. POST /bills/new — X-Idempotency-Key header forwarded
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_sends_idempotency_key(respx_mock: respx.MockRouter) -> None:
    """POST /bills/new passes the idempotency_key field as X-Idempotency-Key header."""
    _idem_key = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("x-idempotency-key", ""))
        return Response(201, json=_MOCK_BILL)

    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/bills/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "idempotency_key": _idem_key,
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert len(captured) == 1, "Expected exactly one upstream POST call"
    assert captured[0] == _idem_key, f"Expected {_idem_key!r}, got {captured[0]!r}"


# ---------------------------------------------------------------------------
# 7. POST /bills/new — requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bill_create_requires_auth() -> None:
    """POST /bills/new without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={"contact_id": _CONTACT_ID, "issue_date": "2026-04-23"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 8. GET /bills/_add_line — requires auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bill_add_line_requires_auth() -> None:
    """GET /bills/_add_line without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/bills/_add_line?index=0")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 9. POST /bills/new — API returns 401 -> session cleared, redirect /login
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_api_401(respx_mock: respx.MockRouter) -> None:
    """POST /bills/new when the API returns 401 clears session and redirects /login."""
    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(401, json={"detail": "Unauthorised"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "idempotency_key": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 10. POST /bills/new — API 500 -> generic error re-rendered on form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_api_500(respx_mock: respx.MockRouter) -> None:
    """POST /bills/new when the API returns 500 re-renders the form with a generic error."""
    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(500, json={"detail": "Internal Server Error"})
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "idempotency_key": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 500
    assert 'name="contact_id"' in resp.text
    assert "API error: HTTP 500" in resp.text


# ---------------------------------------------------------------------------
# 11. POST /bills/new — multiple lines are passed through correctly
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_multi_line(respx_mock: respx.MockRouter) -> None:
    """POST /bills/new with 3 line items -> 201 from API -> 303 redirect."""
    captured_bodies: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured_bodies.append(request.read())
        return Response(201, json=_MOCK_BILL)

    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "idempotency_key": "ffffffff-ffff-ffff-ffff-000000000000",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "2",
                "lines[0][unit_price]": "100.00",
                "lines[1][account_id]": _ACCOUNT_ID,
                "lines[1][description]": "Stationery",
                "lines[1][quantity]": "1",
                "lines[1][unit_price]": "50.00",
                "lines[2][account_id]": _ACCOUNT_ID,
                "lines[2][description]": "Postage",
                "lines[2][quantity]": "4",
                "lines[2][unit_price]": "5.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"
    # Exactly one API call was made.
    assert len(captured_bodies) == 1


# ---------------------------------------------------------------------------
# 12. POST /bills/new — API 422 with string detail shows that message
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_create_api_error_string_detail(respx_mock: respx.MockRouter) -> None:
    """POST /bills/new: API 422 with string detail -> error message shown in form."""
    respx_mock.post(f"{_API_BASE}/api/v1/bills").mock(
        return_value=Response(422, json={"detail": "Duplicate bill number"})
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/bills/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "due_date": "2026-05-23",
                "number": "BILL-0001",
                "idempotency_key": "99999999-9999-9999-9999-999999999999",
                "lines[0][description]": "Office supplies",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 422
    assert 'name="contact_id"' in resp.text
    assert "Duplicate bill number" in resp.text
