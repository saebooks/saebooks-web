"""Tests for the reclassifications list, create, detail, and reverse views.

Ten tests:
1.  test_reclassifications_requires_auth            — 303 -> /login without session
2.  test_reclassifications_list_renders_row          — full-page render contains a row
3.  test_reclassifications_list_partial_htmx         — HX-Request returns fragment (no <html>)
4.  test_reclassification_new_requires_auth          — GET /reclassifications/new without session -> 303
5.  test_reclassification_new_form_renders           — GET /reclassifications/new returns 200 with the form
6.  test_reclassification_new_prefills_query_params  — deep-link with from_account_id + source_entry_id
7.  test_reclassification_create_success_redirects   — POST 201 -> 303 to /reclassifications/{id}
8.  test_reclassification_create_business_error      — POST 400 (cross-side accounts) -> re-render with message
9.  test_reclassification_detail_renders             — detail shows amount, accounts, reverse button
10. test_reclassification_reverse_redirects_with_flash — POST /{id}/reverse -> 303 with flash
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

_RECLASS_ID = "11111111-1111-1111-1111-111111111111"
_FROM_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_TO_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
_COMPANY_ID = "44444444-4444-4444-4444-444444444444"
_JE_ID = "55555555-5555-5555-5555-555555555555"
_SOURCE_JE_ID = "66666666-6666-6666-6666-666666666666"

_MOCK_FROM_ACCOUNT = {"id": _FROM_ACCOUNT_ID, "code": "6-1000", "name": "Motor Vehicle Expenses", "account_type": "EXPENSE"}
_MOCK_TO_ACCOUNT = {"id": _TO_ACCOUNT_ID, "code": "6-1100", "name": "Fuel", "account_type": "EXPENSE"}

_MOCK_RECLASS = {
    "id": _RECLASS_ID,
    "company_id": _COMPANY_ID,
    "from_account_id": _FROM_ACCOUNT_ID,
    "to_account_id": _TO_ACCOUNT_ID,
    "amount": "84.50",
    "reclass_date": "2026-06-06",
    "reason": "Fuel coded to general MV expenses",
    "source_entry_id": _SOURCE_JE_ID,
    "journal_entry_id": _JE_ID,
    "status": "POSTED",
    "created_by": "web:tester",
    "created_at": "2026-06-06T00:00:00Z",
    "updated_at": "2026-06-06T00:00:00Z",
}

_MOCK_RECLASS_RESPONSE = {"items": [_MOCK_RECLASS]}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _mock_accounts(respx_mock: respx.MockRouter) -> None:
    """Register the single all-accounts call reclassifications.py fetches."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts", params={"limit": "1000", "offset": "0"}).mock(
        return_value=Response(
            200, json={"items": [_MOCK_FROM_ACCOUNT, _MOCK_TO_ACCOUNT], "total": 2}
        )
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reclassifications_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/reclassifications")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_reclassifications_list_renders_row(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/reclassifications").mock(
        return_value=Response(200, json=_MOCK_RECLASS_RESPONSE)
    )
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reclassifications")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Fuel coded to general MV expenses" in resp.text
    assert "6-1000" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_reclassifications_list_partial_htmx(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/reclassifications").mock(
        return_value=Response(200, json=_MOCK_RECLASS_RESPONSE)
    )
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reclassifications", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "Fuel coded to general MV expenses" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reclassification_new_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/reclassifications/new")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_reclassification_new_form_renders(respx_mock: respx.MockRouter) -> None:
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reclassifications/new")

    assert resp.status_code == 200
    assert 'name="from_account_id"' in resp.text
    assert 'name="to_account_id"' in resp.text
    assert 'name="amount"' in resp.text
    assert 'name="reclass_date"' in resp.text
    assert "6-1000" in resp.text
    assert "6-1100" in resp.text
    # No-manual-JE messaging in the form guidance.
    assert "journal entry" in resp.text.lower()


@pytest.mark.anyio
@respx.mock
async def test_reclassification_new_prefills_query_params(respx_mock: respx.MockRouter) -> None:
    """Deep-links from a JE / account page can pre-fill the source account
    and original entry."""
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reclassifications/new",
            params={
                "from_account_id": _FROM_ACCOUNT_ID,
                "source_entry_id": _SOURCE_JE_ID,
            },
        )

    assert resp.status_code == 200
    assert f'value="{_SOURCE_JE_ID}"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_reclassification_create_success_redirects(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/reclassifications").mock(
        return_value=Response(201, json=_MOCK_RECLASS)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/reclassifications/new",
            data={
                "from_account_id": _FROM_ACCOUNT_ID,
                "to_account_id": _TO_ACCOUNT_ID,
                "amount": "84.50",
                "reclass_date": "2026-06-06",
                "reason": "Fuel coded to general MV expenses",
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/reclassifications/{_RECLASS_ID}"


@pytest.mark.anyio
@respx.mock
async def test_reclassification_create_business_error_rerenders(respx_mock: respx.MockRouter) -> None:
    """The engine rejects a cross-side pair with 400 + nested {"code","detail"}
    — the form re-renders with the engine's message, not a generic 400 text."""
    respx_mock.post(f"{_API_BASE}/api/v1/reclassifications").mock(
        return_value=Response(
            400,
            json={"detail": {"code": "reclassification_invalid", "detail": "accounts must share the same natural balance side"}},
        )
    )
    _mock_accounts(respx_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/reclassifications/new",
            data={
                "from_account_id": _FROM_ACCOUNT_ID,
                "to_account_id": _TO_ACCOUNT_ID,
                "amount": "10.00",
                "reclass_date": "2026-06-06",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert resp.status_code == 400
    assert "natural balance side" in resp.text


# ---------------------------------------------------------------------------
# Detail + reverse
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reclassification_detail_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/reclassifications/{_RECLASS_ID}").mock(
        return_value=Response(200, json=_MOCK_RECLASS)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_FROM_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_FROM_ACCOUNT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts/{_TO_ACCOUNT_ID}").mock(
        return_value=Response(200, json=_MOCK_TO_ACCOUNT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/reclassifications/{_RECLASS_ID}")

    assert resp.status_code == 200
    assert "84.50" in resp.text
    assert "6-1000" in resp.text
    assert "6-1100" in resp.text
    # Links to both the original and the correction entry.
    assert f"/journal-entries/{_SOURCE_JE_ID}" in resp.text
    assert f"/journal-entries/{_JE_ID}" in resp.text
    # Reverse action present for a POSTED reclassification.
    assert f"/reclassifications/{_RECLASS_ID}/reverse" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_reclassification_reverse_redirects_with_flash(respx_mock: respx.MockRouter) -> None:
    reversed_reclass = {**_MOCK_RECLASS, "status": "REVERSED"}
    respx_mock.post(f"{_API_BASE}/api/v1/reclassifications/{_RECLASS_ID}/reverse").mock(
        return_value=Response(200, json=reversed_reclass)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/reclassifications/{_RECLASS_ID}/reverse")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/reclassifications/{_RECLASS_ID}"
