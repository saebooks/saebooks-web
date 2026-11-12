"""Tests for payment POST and VOID transition actions.

Five tests:
1. test_payment_post_happy_path       — POST /payments/{id}/post; API 200 -> 303 to detail with flash
2. test_payment_post_conflict         — API 409 -> 303 back to detail with conflict flash
3. test_payment_post_validation_error — API 422 -> 303 back to detail with API error flash
4. test_payment_void_happy_path       — POST /payments/{id}/void; API 200 -> 303 to detail with flash
5. test_payment_void_button_not_shown_for_draft — void button absent on DRAFT payment detail;
                                                  post button present on DRAFT detail
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

_PAYMENT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"

_MOCK_PAYMENT_DRAFT = {
    "id": _PAYMENT_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "payment_date": "2026-04-25",
    "amount": "250.00",
    "direction": "INCOMING",
    "method": "eft",
    "currency": "AUD",
    "reference": "PAY-002",
    "notes": None,
    "bank_account_id": None,
    "status": "DRAFT",
    "number": None,
    "version": 1,
    "created_at": "2026-04-25T00:00:00Z",
    "updated_at": "2026-04-25T00:00:00Z",
    "archived_at": None,
    "allocations": [],
}

_MOCK_PAYMENT_POSTED = {**_MOCK_PAYMENT_DRAFT, "status": "POSTED", "version": 2}

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# 1. Happy path — POST /payments/{id}/post; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_post_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /payments/{id}/post; API 200 -> 303 redirect to detail with 'Payment posted.' flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}/post").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_POSTED)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_POSTED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"


# ---------------------------------------------------------------------------
# 2. Conflict — API 409 -> 303 back to detail with conflict flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_post_conflict(respx_mock: respx.MockRouter) -> None:
    """POST with stale version; API 409 -> 303 back to detail with conflict flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}/post").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/post",
            data={"version": "0"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"

    # Follow the redirect and verify the flash text appears in the rendered page.
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_DRAFT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/payments/{_PAYMENT_ID}/post",
            data={"version": "0"},
        )
    assert "Version conflict" in resp2.text


# ---------------------------------------------------------------------------
# 3. Validation error — API 422 -> 303 back to detail with API error as flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_post_validation_error(respx_mock: respx.MockRouter) -> None:
    """API 422 business-rule rejection -> 303 back to detail with message as flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}/post").mock(
        return_value=Response(
            422, json={"detail": "Payment has no allocations and cannot be posted."}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"

    # Follow the redirect and verify the flash message appears.
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_DRAFT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp2 = await client.post(
            f"/payments/{_PAYMENT_ID}/post",
            data={"version": "1"},
        )
    assert "Payment has no allocations" in resp2.text


# ---------------------------------------------------------------------------
# 4. Happy path — void; API 200 -> 303 to detail + flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_void_happy_path(respx_mock: respx.MockRouter) -> None:
    """POST /payments/{id}/void; API 200 -> 303 redirect to detail with 'Payment voided.' flash."""
    _MOCK_PAYMENT_VOIDED = {**_MOCK_PAYMENT_POSTED, "status": "VOIDED", "version": 3}

    void = respx_mock.delete(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(204)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_VOIDED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/payments/{_PAYMENT_ID}/void",
            data={"version": "2"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"
    assert void.called
    assert void.calls.last.request.headers["If-Match"] == "2"


# ---------------------------------------------------------------------------
# 5. Void button absent on DRAFT; Post button present on DRAFT
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_void_button_not_shown_for_draft(
    respx_mock: respx.MockRouter,
) -> None:
    """Detail page for a DRAFT payment must not render the void form; Post form must be present."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_DRAFT)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/payments/{_PAYMENT_ID}")

    assert resp.status_code == 200
    # Void form must not be present for DRAFT payments.
    assert f"/payments/{_PAYMENT_ID}/void" not in resp.text
    # Post button MUST be present for DRAFT payments.
    assert f"/payments/{_PAYMENT_ID}/post" in resp.text


# ---------------------------------------------------------------------------
# Payment-form fix (2026-11-12): void = DELETE + If-Match; errors in flash;
# bulk void sends the fetched version.
# ---------------------------------------------------------------------------


def _flash_of(resp) -> str:
    """Decode the flash message out of the signed session cookie."""
    raw = resp.cookies.get(settings.session_cookie_name)
    assert raw, "no session cookie set"
    signer = _TimestampSigner(settings.secret_key)
    from base64 import b64decode as _b64decode
    return _json.loads(_b64decode(signer.unsign(raw))).get("flash", "")


@pytest.mark.anyio
@respx.mock
async def test_payment_void_api_error_shown_in_flash(respx_mock: respx.MockRouter) -> None:
    """A refused void (period lock 422) reaches the user, verbatim."""
    respx_mock.delete(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(422, json={"detail": "period 2026-06 is locked"})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/payments/{_PAYMENT_ID}/void", data={"version": "2"})
    assert resp.status_code == 303
    assert "period 2026-06 is locked" in _flash_of(resp)


@pytest.mark.anyio
@respx.mock
async def test_payment_void_fetches_version_when_form_has_none(
    respx_mock: respx.MockRouter,
) -> None:
    """No version in the form: the current version is fetched and sent."""
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_POSTED)
    )
    void = respx_mock.delete(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(204)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/payments/{_PAYMENT_ID}/void", data={})
    assert resp.status_code == 303
    assert void.calls.last.request.headers["If-Match"] == "2"
    assert _flash_of(resp) == "Payment voided."


@pytest.mark.anyio
@respx.mock
async def test_bulk_void_uses_delete_with_if_match(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(200, json=_MOCK_PAYMENT_POSTED)
    )
    void = respx_mock.delete(f"{_API_BASE}/api/v1/payments/{_PAYMENT_ID}").mock(
        return_value=Response(204)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/payments/bulk", data={"action": "void", "ids[]": [_PAYMENT_ID]}
        )
    assert resp.status_code == 303
    assert void.called
    assert void.calls.last.request.headers["If-Match"] == "2"
    assert "1 payment processed" in _flash_of(resp)
