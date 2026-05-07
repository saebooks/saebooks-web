"""Tests for the tax code create form — Lane D cycle 24.

Five tests:
1. test_tax_code_new_form_renders            — GET /tax-codes/new returns form with all fields
2. test_tax_code_create_success_redirects    — POST happy path -> 303 to /tax-codes/{id}
3. test_tax_code_create_validation_error     — POST 422 -> re-render form with errors
4. test_tax_code_create_duplicate_code       — POST 422 string detail -> __all__ banner
5. test_tax_code_create_selects_rendered     — tax_system + reporting_type selects have expected options
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
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TAX_CODE_ID = "aaaaaaaa-2424-2424-2424-aaaaaaaaaaaa"

_MOCK_TAX_CODE = {
    "id": _TAX_CODE_ID,
    "company_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "code": "GST",
    "name": "GST on Sales",
    "rate": "10.0",
    "tax_system": "GST",
    "reporting_type": "taxable",
    "description": None,
    "version": 1,
    "created_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /tax-codes/new — form renders with all expected fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tax_code_new_form_renders() -> None:
    """GET /tax-codes/new returns the form with all expected fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/tax-codes/new")

    assert resp.status_code == 200
    assert 'name="code"' in resp.text
    assert 'name="name"' in resp.text
    assert 'name="rate"' in resp.text
    assert 'name="tax_system"' in resp.text
    assert 'name="reporting_type"' in resp.text
    assert 'name="description"' in resp.text
    assert 'name="idempotency_key"' in resp.text


# ---------------------------------------------------------------------------
# 2. POST /tax-codes/new happy path -> 303 redirect to /tax-codes/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_code_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /tax-codes/new with valid data mocks a 201 response and returns 303."""
    respx_mock.post(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(201, json=_MOCK_TAX_CODE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/tax-codes/new",
            data={
                "code": "GST",
                "name": "GST on Sales",
                "rate": "10",
                "tax_system": "GST",
                "reporting_type": "taxable",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/tax-codes/{_TAX_CODE_ID}"


# ---------------------------------------------------------------------------
# 3. POST /tax-codes/new — 422 per-field validation error -> re-render
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_code_create_validation_error(respx_mock: respx.MockRouter) -> None:
    """POST /tax-codes/new where API returns 422 re-renders the form with errors."""
    _422_body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "code"],
                "msg": "Field required",
                "input": {},
            }
        ]
    }
    respx_mock.post(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(422, json=_422_body)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/tax-codes/new",
            data={
                "name": "No Code Tax",
                "rate": "10",
                "tax_system": "GST",
                "reporting_type": "taxable",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert resp.status_code == 422
    # Form re-rendered, not a blank page.
    assert 'name="name"' in resp.text
    # Submitted name preserved.
    assert "No Code Tax" in resp.text
    # Error text visible.
    assert "Field required" in resp.text


# ---------------------------------------------------------------------------
# 4. POST /tax-codes/new — 422 with string detail (duplicate code) -> __all__ banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_code_create_duplicate_code(respx_mock: respx.MockRouter) -> None:
    """POST /tax-codes/new where API returns a plain string 422 detail -> __all__ banner."""
    respx_mock.post(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(422, json={"detail": "Tax code already exists for this company."})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/tax-codes/new",
            data={
                "code": "GST",
                "name": "Duplicate GST",
                "rate": "10",
                "tax_system": "GST",
                "reporting_type": "taxable",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        )

    assert resp.status_code == 422
    # Non-field error banner should show the API message.
    assert "Tax code already exists" in resp.text
    # Submitted code preserved.
    assert "Duplicate GST" in resp.text


# ---------------------------------------------------------------------------
# 5. GET /tax-codes/new — tax_system + reporting_type selects rendered
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tax_code_create_selects_rendered() -> None:
    """GET /tax-codes/new renders tax_system and reporting_type selects with expected options."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/tax-codes/new")

    assert resp.status_code == 200
    # tax_system options
    for value in ("GST", "VAT", "other"):
        assert f'value="{value}"' in resp.text, f"Missing tax_system option: {value}"
    # reporting_type options
    for value in ("taxable", "gst_free", "input_taxed", "out_of_scope", "exempt"):
        assert f'value="{value}"' in resp.text, f"Missing reporting_type option: {value}"
