"""Tests for bad-debt recovery detection + recording — Phase 2 / Task 11 + 12.

Covers:
  * smart_prompt detection redirects a receipt from a written-off payer to
    the recovery prompt
  * manual mode does NOT prompt
  * the prompt screen lists the payer's WRITTEN_OFF invoices
  * recording a recovery proxies to engine /record-recovery (201 -> flash)
  * a 409 from the engine surfaces as an error flash, no crash
All engine HTTP is mocked.
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

_API_BASE = settings.api_url.rstrip("/")
_COMPANY_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_WO_INVOICE_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_BANK_ID = "99999999-9999-9999-9999-999999999999"
_PAYMENT_ID = "77777777-7777-7777-7777-777777777777"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "TEST_SESSION_TOKEN"})


def _company(recovery_mode: str = "smart_prompt") -> dict:
    return {
        "id": _COMPANY_ID, "name": "Acme Pty Ltd", "version": 3,
        "writeoff_mode": "review", "writeoff_threshold_days": 90,
        "recovery_mode": recovery_mode, "bad_debt_recovery_account": None,
    }


_WO_INVOICE = {
    "id": _WO_INVOICE_ID, "number": "INV-0042", "status": "WRITTEN_OFF",
    "total": "500.00", "amount_paid": "0.00", "issue_date": "2025-01-10",
    "contact_id": _CONTACT_ID,
}


# ---------------------------------------------------------------------------
# 1. Payment-create from a written-off payer (smart_prompt) → redirect to prompt
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_incoming_writtenoff_payer_redirects_to_prompt(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json={"id": _PAYMENT_ID})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [_company("smart_prompt")], "total": 1})
    )
    # Detection queries WRITTEN_OFF invoices for the payer.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [_WO_INVOICE], "total": 1})
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
                "contact_id": _CONTACT_ID,
                "direction": "INCOMING",
                "amount": "500.00",
                "payment_date": "2026-06-23",
                "bank_account_id": _BANK_ID,
            },
        )

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/bad-debts/recovery/prompt")
    assert f"contact_id={_CONTACT_ID}" in loc
    assert "amount=500.00" in loc


# ---------------------------------------------------------------------------
# 2. manual recovery_mode → NO prompt, normal redirect to payment detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payment_manual_mode_no_prompt(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(201, json={"id": _PAYMENT_ID})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [_company("manual")], "total": 1})
    )
    # Even if there were written-off invoices, manual mode must not query/prompt;
    # provide a mock so an accidental call wouldn't 404, but assert no redirect.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [_WO_INVOICE], "total": 1})
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
                "contact_id": _CONTACT_ID,
                "direction": "INCOMING",
                "amount": "500.00",
                "payment_date": "2026-06-23",
                "bank_account_id": _BANK_ID,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"


# ---------------------------------------------------------------------------
# 3. Prompt screen lists the payer's WRITTEN_OFF invoices
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_recovery_prompt_lists_writtenoff(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [_WO_INVOICE], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/bad-debts/recovery/prompt",
            params={
                "contact_id": _CONTACT_ID,
                "amount": "500.00",
                "bank_account_id": _BANK_ID,
                "payment_id": _PAYMENT_ID,
            },
        )

    assert resp.status_code == 200
    assert "INV-0042" in resp.text
    assert "Recover $500.00" in resp.text
    # The record form posts the chosen invoice id.
    assert f'value="{_WO_INVOICE_ID}"' in resp.text


# ---------------------------------------------------------------------------
# 4. Prompt with no written-off invoices → bounce to payment detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_recovery_prompt_no_writtenoff_bounces(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get(
            "/bad-debts/recovery/prompt",
            params={
                "contact_id": _CONTACT_ID, "amount": "500.00",
                "bank_account_id": _BANK_ID, "payment_id": _PAYMENT_ID,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/payments/{_PAYMENT_ID}"


# ---------------------------------------------------------------------------
# 5. Record a recovery → engine /record-recovery called, 201 → redirect+flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_recovery_record_calls_engine(respx_mock: respx.MockRouter) -> None:
    captured: list[dict] = []

    def _capture(request: respx.Request, *_: object) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json={
            "journal_entry_id": "je-1", "invoice_id": _WO_INVOICE_ID,
            "amount": "500.00", "recovery_date": "2026-06-23",
            "bank_account_id": _BANK_ID,
        })

    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_WO_INVOICE_ID}/record-recovery"
    ).mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bad-debts/recovery/record",
            data={
                "invoice_id": _WO_INVOICE_ID,
                "bank_account_id": _BANK_ID,
                "amount": "500.00",
                "contact_id": _CONTACT_ID,
                "recovery_date": "2026-06-23",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_WO_INVOICE_ID}"
    assert captured, "record-recovery endpoint not called"
    body = captured[0]
    assert body["bank_account_id"] == _BANK_ID
    assert body["amount"] == "500.00"
    assert body["payer_contact_id"] == _CONTACT_ID
    assert body["recovery_date"] == "2026-06-23"


# ---------------------------------------------------------------------------
# 6. Engine 409 on record → error flash, redirect (no crash)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_recovery_record_conflict(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_WO_INVOICE_ID}/record-recovery"
    ).mock(return_value=Response(409, json={"detail": "Invoice is not WRITTEN_OFF"}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/bad-debts/recovery/record",
            data={
                "invoice_id": _WO_INVOICE_ID,
                "bank_account_id": _BANK_ID,
                "amount": "500.00",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_WO_INVOICE_ID}"
