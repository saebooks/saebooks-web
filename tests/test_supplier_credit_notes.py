"""Tests for the supplier credit notes list, create, detail, post, and void
views — the purchase-side mirror of credit_notes.py.

Nine tests:
1. test_scn_requires_auth                — 303 -> /login without session
2. test_scn_list_renders_row             — full-page render contains an SCN row
3. test_scn_list_partial_htmx            — HX-Request returns fragment (no <html>)
4. test_scn_new_requires_auth            — GET /supplier-credit-notes/new -> 303
5. test_scn_new_form_renders             — GET /supplier-credit-notes/new returns 200 with the form
6. test_scn_create_success_redirects     — POST 201 -> 303 to /supplier-credit-notes/{id}
7. test_scn_create_validation_error      — API 422 -> re-render with errors
8. test_scn_detail_renders_applied_to_bill — detail shows the bill link, not an invoice link
9. test_scn_void_returns_200_not_204     — POST /{id}/void handles the engine's 200 (not 204) response
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

_SCN_ID = "11111111-1111-1111-1111-111111111111"
_BILL_ID = "22222222-2222-2222-2222-222222222222"
_CONTACT_ID = "33333333-3333-3333-3333-333333333333"
_ACCOUNT_ID = "44444444-4444-4444-4444-444444444444"
_TAX_CODE_ID = "55555555-5555-5555-5555-555555555555"

_MOCK_SUPPLIER = {"id": _CONTACT_ID, "name": "Timber Supplies Pty Ltd", "contact_type": "SUPPLIER"}
_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "code": "5-1000", "name": "Materials", "account_type": "EXPENSE"}
_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}

_MOCK_SCN = {
    "id": _SCN_ID,
    "company_id": "66666666-6666-6666-6666-666666666666",
    "contact_id": _CONTACT_ID,
    "number": "SCN-0001",
    "issue_date": "2026-06-06",
    "status": "DRAFT",
    "original_bill_id": _BILL_ID,
    "supplier_reference": "THEIR-CN-99",
    "subtotal": "200.00",
    "tax_total": "20.00",
    "total": "220.00",
    "reason": "Return of goods",
    "notes": None,
    "journal_entry_id": None,
    "void_journal_entry_id": None,
    "version": 1,
    "created_at": "2026-06-06T00:00:00Z",
    "updated_at": "2026-06-06T00:00:00Z",
    "lines": [
        {
            "id": "77777777-7777-7777-7777-777777777777",
            "line_no": 1,
            "description": "Returned timber offcuts",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "2.0",
            "unit_price": "100.00",
            "discount_pct": "0.0",
            "line_subtotal": "200.00",
            "line_tax": "20.00",
            "line_total": "220.00",
        }
    ],
}

_MOCK_SCNS_RESPONSE = {"items": [_MOCK_SCN], "total": 1, "limit": 50, "offset": 0}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_MOCK_SUPPLIER], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [_MOCK_ACCOUNT], "total": 1})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": [_MOCK_TAX_CODE], "total": 1})
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scn_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/supplier-credit-notes")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_scn_list_renders_row(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/supplier_credit_notes").mock(
        return_value=Response(200, json=_MOCK_SCNS_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_MOCK_SUPPLIER], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/supplier-credit-notes")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "SCN-0001" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_scn_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/supplier_credit_notes").mock(
        return_value=Response(200, json=_MOCK_SCNS_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/supplier-credit-notes", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "SCN-0001" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scn_new_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/supplier-credit-notes/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_scn_new_form_renders(respx_mock: respx.MockRouter) -> None:
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/supplier-credit-notes/new")

    assert resp.status_code == 200
    assert 'name="contact_id"' in resp.text
    assert 'name="issue_date"' in resp.text
    assert 'name="original_bill_id"' in resp.text
    assert 'name="supplier_reference"' in resp.text
    assert 'name="lines[0][description]"' in resp.text
    assert "Timber Supplies Pty Ltd" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_scn_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/supplier_credit_notes").mock(
        return_value=Response(201, json=_MOCK_SCN)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/supplier-credit-notes/new",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-06-06",
                "original_bill_id": _BILL_ID,
                "supplier_reference": "THEIR-CN-99",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Returned timber offcuts",
                "lines[0][quantity]": "2",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/supplier-credit-notes/{_SCN_ID}"


@pytest.mark.anyio
@respx.mock
async def test_scn_create_validation_error(respx_mock: respx.MockRouter) -> None:
    _422_body = {
        "detail": [
            {"type": "missing", "loc": ["body", "contact_id"], "msg": "Field required", "input": {}}
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/supplier_credit_notes").mock(
        return_value=Response(422, json=_422_body)
    )
    _mock_dropdowns(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/supplier-credit-notes/new",
            data={
                "issue_date": "2026-06-06",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "lines[0][description]": "Returned timber offcuts",
                "lines[0][quantity]": "2",
                "lines[0][unit_price]": "100.00",
            },
        )

    assert resp.status_code == 422
    assert 'name="contact_id"' in resp.text
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_scn_detail_renders_applied_to_bill(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/supplier_credit_notes/{_SCN_ID}").mock(
        return_value=Response(200, json=_MOCK_SCN)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/supplier-credit-notes/{_SCN_ID}")

    assert resp.status_code == 200
    assert "SCN-0001" in resp.text
    # Applied-to a BILL, not an invoice.
    assert f"/bills/{_BILL_ID}" in resp.text
    assert "Returned timber offcuts" in resp.text


# ---------------------------------------------------------------------------
# Void
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_scn_void_returns_200_not_204(respx_mock: respx.MockRouter) -> None:
    """SCN's void endpoint returns 200 with the updated record — NOT the 204
    No Content that credit_notes' void returns."""
    voided = {**_MOCK_SCN, "status": "VOIDED", "version": 2}
    respx_mock.post(f"{_API_BASE}/api/v1/supplier_credit_notes/{_SCN_ID}/void").mock(
        return_value=Response(200, json=voided)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/supplier-credit-notes/{_SCN_ID}/void", data={"version": "1"})

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/supplier-credit-notes/{_SCN_ID}"
