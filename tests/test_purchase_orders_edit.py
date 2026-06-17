"""Tests for the purchase-order edit form (W35 — DRAFT-only edit).

1. test_po_edit_requires_auth         — GET /purchase_orders/{id}/edit no session -> 303 /login
2. test_po_edit_form_renders_for_draft — DRAFT PO -> form with version hidden + lines + header
3. test_po_edit_redirects_non_draft    — OPEN PO -> 303 back to detail (flash, no form)
4. test_po_edit_success_redirects      — POST + PATCH 200 -> 303 to detail
5. test_po_edit_sends_if_match_header  — outbound PATCH carries If-Match: <version>
6. test_po_edit_validation_error       — PATCH 422 -> form re-renders with __all__ error
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

_PO_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"

_MOCK_CONTACTS = {"items": [{"id": _CONTACT_ID, "name": "Acme Supplies", "contact_type": "SUPPLIER"}], "total": 1}
_MOCK_ACCOUNTS = {"items": [{"id": _ACCOUNT_ID, "name": "Office Supplies", "code": "6200", "account_type": "EXPENSE"}], "total": 1}
_MOCK_TAX_CODES = {"items": [{"id": _TAX_CODE_ID, "name": "GST", "rate": "0.10"}], "total": 1}
_MOCK_PROJECTS = {"items": [], "total": 0}

_MOCK_PO_DRAFT = {
    "id": _PO_ID,
    "contact_id": _CONTACT_ID,
    "number": "PO-0001",
    "issue_date": "2026-04-23",
    "expected_date": "2026-05-07",
    "delivery_address": "1 Test St",
    "notes": None,
    "status": "DRAFT",
    "currency": "AUD",
    "fx_rate": "1.0",
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "version": 2,
    "lines": [
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "line_no": 1,
            "description": "Drill bits",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "1.0",
            "unit_price": "100.00",
            "project_id": None,
        }
    ],
}
_MOCK_PO_OPEN = {**_MOCK_PO_DRAFT, "status": "OPEN", "version": 3}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode("utf-8"))).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(return_value=Response(200, json=_MOCK_CONTACTS))
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(return_value=Response(200, json=_MOCK_ACCOUNTS))
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(return_value=Response(200, json=_MOCK_TAX_CODES))
    respx_mock.get(f"{_API_BASE}/api/v1/projects").mock(return_value=Response(200, json=_MOCK_PROJECTS))


@pytest.mark.anyio
async def test_po_edit_requires_auth() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False) as client:
        resp = await client.get(f"/purchase_orders/{_PO_ID}/edit")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_po_edit_form_renders_for_draft(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/purchase_orders/{_PO_ID}").mock(return_value=Response(200, json=_MOCK_PO_DRAFT))
    _mock_dropdowns(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _SESSION_COOKIE}) as client:
        resp = await client.get(f"/purchase_orders/{_PO_ID}/edit")
    assert resp.status_code == 200
    assert 'name="version"' in resp.text
    assert 'value="2"' in resp.text
    assert 'name="idempotency_key"' in resp.text
    assert "Drill bits" in resp.text
    assert "2026-04-23" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_po_edit_redirects_non_draft(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/purchase_orders/{_PO_ID}").mock(return_value=Response(200, json=_MOCK_PO_OPEN))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _SESSION_COOKIE},
                           follow_redirects=False) as client:
        resp = await client.get(f"/purchase_orders/{_PO_ID}/edit")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/purchase_orders/{_PO_ID}"


@pytest.mark.anyio
@respx.mock
async def test_po_edit_success_redirects(respx_mock: respx.MockRouter) -> None:
    respx_mock.patch(f"{_API_BASE}/api/v1/purchase_orders/{_PO_ID}").mock(return_value=Response(200, json=_MOCK_PO_DRAFT))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _SESSION_COOKIE},
                           follow_redirects=False) as client:
        resp = await client.post(
            f"/purchase_orders/{_PO_ID}/edit",
            data={
                "contact_id": _CONTACT_ID,
                "issue_date": "2026-04-23",
                "expected_date": "2026-05-07",
                "version": "2",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "lines[0][account_id]": _ACCOUNT_ID,
                "lines[0][description]": "Drill bits",
                "lines[0][quantity]": "1",
                "lines[0][unit_price]": "100.00",
            },
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/purchase_orders/{_PO_ID}"


@pytest.mark.anyio
@respx.mock
async def test_po_edit_sends_if_match_header(respx_mock: respx.MockRouter) -> None:
    captured: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(request.headers.get("if-match", ""))
        return Response(200, json=_MOCK_PO_DRAFT)

    respx_mock.patch(f"{_API_BASE}/api/v1/purchase_orders/{_PO_ID}").mock(side_effect=_capture)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _SESSION_COOKIE},
                           follow_redirects=False) as client:
        await client.post(
            f"/purchase_orders/{_PO_ID}/edit",
            data={"contact_id": _CONTACT_ID, "issue_date": "2026-04-23", "expected_date": "2026-05-07",
                  "version": "2", "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                  "lines[0][account_id]": _ACCOUNT_ID, "lines[0][description]": "Drill bits",
                  "lines[0][quantity]": "1", "lines[0][unit_price]": "100.00"},
        )
    assert captured == ["2"]


@pytest.mark.anyio
@respx.mock
async def test_po_edit_validation_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.patch(f"{_API_BASE}/api/v1/purchase_orders/{_PO_ID}").mock(
        return_value=Response(422, json={"detail": [{"loc": ["body", "issue_date"], "msg": "invalid date"}]})
    )
    _mock_dropdowns(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _SESSION_COOKIE}) as client:
        resp = await client.post(
            f"/purchase_orders/{_PO_ID}/edit",
            data={"contact_id": _CONTACT_ID, "issue_date": "bad", "expected_date": "2026-05-07",
                  "version": "2", "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                  "lines[0][account_id]": _ACCOUNT_ID, "lines[0][description]": "Drill bits",
                  "lines[0][quantity]": "1", "lines[0][unit_price]": "100.00"},
        )
    assert resp.status_code == 422
    assert 'name="version"' in resp.text  # form re-rendered
