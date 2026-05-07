"""Tests for the recurring invoice "Generate invoice now" action — Lane D cycle 38.

Three tests:
1. test_ri_generate_happy     — POST /recurring-invoices/{id}/generate; API 201
                                -> 303 redirect to /invoices/{invoice_id}
2. test_ri_generate_conflict  — API 409 -> 303 back to RI detail with conflict flash
3. test_ri_generate_422       — API 422 -> 303 back to RI detail with error flash

Four additional UI tests:
4. test_ri_generate_button_shown_active   — detail page for ACTIVE RI has generate form
5. test_ri_generate_button_hidden_paused  — detail page for PAUSED RI has no generate form
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

_RI_ID = "bbbbbbbb-bbbb-bbbb-bbbb-ge0000000038"
_INVOICE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-ge0000000038"
_CONTACT_ID = "cccccccc-cccc-cccc-cccc-ge0000000038"

_MOCK_RI_ACTIVE = {
    "id": _RI_ID,
    "company_id": "ffffffff-ffff-ffff-ffff-ge0000000038",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "name": "Generate Test RI",
    "frequency": "MONTHLY",
    "status": "ACTIVE",
    "anchor_day": None,
    "next_run": "2026-05-01",
    "end_date": None,
    "last_run": None,
    "due_days": 30,
    "payment_terms": None,
    "notes": None,
    "auto_post": False,
    "invoices_generated": 3,
    "version": 2,
    "created_at": "2026-04-24T00:00:00Z",
    "updated_at": "2026-04-24T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_RI_PAUSED = {
    **_MOCK_RI_ACTIVE,
    "status": "PAUSED",
}

_GENERATE_RESPONSE = {
    "invoice_id": _INVOICE_ID,
    "id": _INVOICE_ID,
}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Happy path — API 201 -> 303 redirect to /invoices/{invoice_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_generate_happy(respx_mock: respx.MockRouter) -> None:
    """POST /recurring-invoices/{id}/generate; API 201 -> 303 redirect to generated invoice."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}/generate"
    ).mock(return_value=Response(201, json=_GENERATE_RESPONSE))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/generate",
            data={"version": "2", "idempotency_key": "test-idem-key-001"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 2. Conflict — API 409 -> 303 back to RI detail with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_generate_conflict(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to RI detail with version conflict flash."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}/generate"
    ).mock(return_value=Response(409, json={"detail": "Version conflict"}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/generate",
            data={"version": "1", "idempotency_key": "test-idem-key-002"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/recurring-invoices/{_RI_ID}"


# ---------------------------------------------------------------------------
# 3. Validation error — API 422 -> 303 back to RI detail with error flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_generate_422(respx_mock: respx.MockRouter) -> None:
    """POST with invalid state; API 422 -> 303 back to RI detail with error flash."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}/generate"
    ).mock(
        return_value=Response(
            422,
            json={"detail": [{"loc": ["body"], "msg": "RI is not ACTIVE", "type": "value_error"}]},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/recurring-invoices/{_RI_ID}/generate",
            data={"version": "2", "idempotency_key": "test-idem-key-003"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/recurring-invoices/{_RI_ID}"


# ---------------------------------------------------------------------------
# 4. Generate button visible on ACTIVE detail page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_generate_button_shown_active(respx_mock: respx.MockRouter) -> None:
    """Detail page for ACTIVE RI shows the generate form."""
    respx_mock.get(
        f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}"
    ).mock(return_value=Response(200, json=_MOCK_RI_ACTIVE))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/recurring-invoices/{_RI_ID}")

    assert resp.status_code == 200
    assert f"/recurring-invoices/{_RI_ID}/generate" in resp.text
    assert "Generate invoice now" in resp.text


# ---------------------------------------------------------------------------
# 5. Generate button hidden on PAUSED detail page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ri_generate_button_hidden_paused(respx_mock: respx.MockRouter) -> None:
    """Detail page for PAUSED RI does not show the generate form."""
    respx_mock.get(
        f"{_API_BASE}/api/v1/recurring_invoices/{_RI_ID}"
    ).mock(return_value=Response(200, json=_MOCK_RI_PAUSED))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/recurring-invoices/{_RI_ID}")

    assert resp.status_code == 200
    assert f"/recurring-invoices/{_RI_ID}/generate" not in resp.text
    assert "Generate invoice now" not in resp.text
