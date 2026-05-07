"""Tests for credit note POST and VOID transition actions — Lane D cycle 39.

Six tests:
1. test_cn_post_success_redirects          — POST /credit-notes/{id}/post; API 200 -> 303 to detail
2. test_cn_post_409_shows_flash            — stale version -> API 409 -> 303 + conflict flash
3. test_cn_post_422_shows_api_message      — API 422 -> 303 + API error message as flash
4. test_cn_void_success_redirects          — POST /credit-notes/{id}/void; API 204 -> 303 to detail
5. test_cn_void_409_shows_flash            — API 409 -> 303 + conflict flash
6. test_cn_void_button_not_shown_for_draft — void button absent on DRAFT credit note detail
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

_CN_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"

_MOCK_CN_DRAFT = {
    "id": _CN_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "CN-0001",
    "issue_date": "2026-04-23",
    "status": "DRAFT",
    "original_invoice_id": None,
    "subtotal": "100.00",
    "tax_total": "10.00",
    "total": "110.00",
    "amount_allocated": "0.00",
    "reason": None,
    "notes": None,
    "posted_at": None,
    "posted_by": None,
    "version": 2,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "archived_at": None,
    "lines": [],
}

_MOCK_CN_POSTED = {**_MOCK_CN_DRAFT, "status": "POSTED", "version": 3}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Happy path — POST /credit-notes/{id}/post; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cn_post_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /credit-notes/{id}/post; API 200 -> 303 redirect to detail with 'Credit note posted.' flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/post").mock(
        return_value=Response(200, json=_MOCK_CN_POSTED)
    )
    # Detail GET (after redirect follows) — needed if follow_redirects=True.
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/post",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"


# ---------------------------------------------------------------------------
# 2. Conflict — API 409 -> 303 back to detail with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cn_post_409_shows_flash(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/post").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"

    # Follow the redirect and verify the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_DRAFT)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/credit-notes/{_CN_ID}/post",
            data={"version": "1"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 3. Validation error — API 422 -> 303 back to detail with API error message
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cn_post_422_shows_api_message(respx_mock: respx.MockRouter) -> None:
    """API 422 (e.g. business rule) -> 303 back to detail with API message as flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/post").mock(
        return_value=Response(
            422, json={"detail": "Credit note has no lines and cannot be posted."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/post",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"

    # Follow the redirect and verify the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_DRAFT)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/credit-notes/{_CN_ID}/post",
            data={"version": "2"},
        )
    assert "Credit note has no lines" in resp2.text


# ---------------------------------------------------------------------------
# 4. Happy path — void; API 204 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cn_void_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /credit-notes/{id}/void; API 204 -> 303 redirect to detail with 'Credit note voided.' flash."""
    _MOCK_CN_VOIDED = {**_MOCK_CN_POSTED, "status": "VOIDED", "version": 4}

    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/void").mock(
        return_value=Response(204)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_VOIDED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/void",
            data={"version": "3"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"


# ---------------------------------------------------------------------------
# 5. Void conflict — API 409 -> 303 back to detail with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cn_void_409_shows_flash(respx_mock: respx.MockRouter) -> None:
    """POST void with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/void").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/credit-notes/{_CN_ID}/void",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CN_ID}"

    # Follow the redirect and verify the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_POSTED)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/credit-notes/{_CN_ID}/void",
            data={"version": "2"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 6. Void button not shown on DRAFT credit note detail page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cn_void_button_not_shown_for_draft(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a DRAFT credit note must not render the void form."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}").mock(
        return_value=Response(200, json=_MOCK_CN_DRAFT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}")

    assert resp.status_code == 200
    # Void form must not be present for DRAFT credit notes.
    assert f"/credit-notes/{_CN_ID}/void" not in resp.text
    # Post button MUST be present for DRAFT credit notes.
    assert f"/credit-notes/{_CN_ID}/post" in resp.text
