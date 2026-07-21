"""Tests for the invoice email-send flow and fixed bulk actions — P0-1/P0-2
of the 2026-07-22 UI gap audit.

Covered:
1. test_email_compose_prefills_from_contact       — GET /invoices/{id}/email
2. test_email_compose_redirects_for_draft_invoice  — POSTED-only gate
3. test_email_send_sent_mode                       — POST relays "sent"
4. test_email_send_blocked_mode                    — POST relays "blocked"
5. test_detail_send_button_only_for_posted          — detail page gating
6. test_detail_record_payment_link_for_outstanding_balance
7. test_bulk_send_mixed_outcomes_reported_honestly  — sent/blocked/skipped/failed
8. test_bulk_send_never_reports_blocked_as_sent     — the mode-vs-status-code trap
9. test_bulk_mark_paid_action_removed               — old action name now rejected
10. test_payments_new_prefills_from_query_params    — /payments/new?invoice_id=...
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

_INVOICE_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


def _invoice(status: str = "POSTED", *, total="110.00", amount_paid="0.00", **overrides) -> dict:
    base = {
        "id": _INVOICE_ID,
        "company_id": "44444444-4444-4444-4444-444444444444",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "contact_id": _CONTACT_ID,
        "number": "INV-0001",
        "issue_date": "2026-04-23",
        "due_date": "2026-05-23",
        "status": status,
        "subtotal": "100.00",
        "tax_total": "10.00",
        "total": total,
        "amount_paid": amount_paid,
        "currency": "AUD",
        "fx_rate": "1.0",
        "notes": None,
        "payment_terms": "Net 30",
        "posted_at": None,
        "posted_by": None,
        "version": 3,
        "created_at": "2026-04-23T00:00:00Z",
        "updated_at": "2026-04-23T00:00:00Z",
        "archived_at": None,
        "lines": [],
    }
    base.update(overrides)
    return base


def _contact(email: str = "customer@example.com") -> dict:
    return {"id": _CONTACT_ID, "name": "Acme Pty Ltd", "email": email, "contact_type": "CUSTOMER"}


# ---------------------------------------------------------------------------
# 1. Compose page prefills To from the contact's email
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_email_compose_prefills_from_contact(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice())
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_contact())
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}/email")

    assert resp.status_code == 200
    assert "customer@example.com" in resp.text
    assert f"/invoices/{_INVOICE_ID}/email" in resp.text  # form action


# ---------------------------------------------------------------------------
# 2. DRAFT invoices cannot be emailed — redirect with flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_email_compose_redirects_for_draft_invoice(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice(status="DRAFT"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_contact())
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}/email")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/invoices/{_INVOICE_ID}"


# ---------------------------------------------------------------------------
# 3 & 4. POST relays the engine's mode faithfully
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_email_send_sent_mode(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/send-email").mock(
        return_value=Response(200, json={
            "mode": "sent", "log_id": "log-1", "message_id": "resend-abc",
            "reason": None, "outbox_path": None,
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice())
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/email",
            data={
                "from_addr": "accounts@saee.com.au",
                "to": "customer@example.com",
                "subject": "Tax Invoice INV-0001",
                "body_html": "<p>hi</p>",
            },
        )

    assert resp.status_code == 200
    assert "Sent" in resp.text
    assert "resend-abc" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_email_send_blocked_mode(respx_mock: respx.MockRouter) -> None:
    """The kill switch blocking a send is not an error — HTTP 200, mode=blocked."""
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/send-email").mock(
        return_value=Response(200, json={
            "mode": "blocked", "log_id": "log-2", "message_id": None,
            "reason": "SAEBOOKS_EMAIL_SEND_ENABLED not set", "outbox_path": "/outbox/x.eml",
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice())
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/email",
            data={
                "from_addr": "accounts@saee.com.au",
                "to": "customer@example.com",
                "subject": "Tax Invoice INV-0001",
                "body_html": "<p>hi</p>",
            },
        )

    assert resp.status_code == 200
    assert "BLOCKED" in resp.text
    assert "SAEBOOKS_EMAIL_SEND_ENABLED" in resp.text


# ---------------------------------------------------------------------------
# 5. Detail page only shows "Send to customer" for POSTED invoices
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_detail_send_button_only_for_posted(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice(status="DRAFT"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(return_value=Response(200, json=[]))
    respx_mock.get(f"{_API_BASE}/api/v1/email-log/by-doc/invoice/{_INVOICE_ID}").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    assert f"/invoices/{_INVOICE_ID}/email" not in resp.text


# ---------------------------------------------------------------------------
# 6. Detail page offers "Record payment" only when a balance is outstanding
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_detail_record_payment_link_for_outstanding_balance(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice(status="POSTED", total="110.00", amount_paid="0.00"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(return_value=Response(200, json=[]))
    respx_mock.get(f"{_API_BASE}/api/v1/email-log/by-doc/invoice/{_INVOICE_ID}").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    assert "/payments/new?" in resp.text
    assert f"invoice_id={_INVOICE_ID}" in resp.text
    assert f"/invoices/{_INVOICE_ID}/email" in resp.text  # send button present too


# ---------------------------------------------------------------------------
# 7 & 8. Bulk send — honest per-row reporting, never conflates blocked/sent
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bulk_send_mixed_outcomes_reported_honestly(respx_mock: respx.MockRouter) -> None:
    sent_id = "10000000-0000-0000-0000-000000000001"
    blocked_id = "20000000-0000-0000-0000-000000000002"
    draft_id = "30000000-0000-0000-0000-000000000003"
    no_email_id = "40000000-0000-0000-0000-000000000004"

    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{sent_id}").mock(
        return_value=Response(200, json=_invoice(id=sent_id, status="POSTED", number="INV-0002"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{blocked_id}").mock(
        return_value=Response(200, json=_invoice(id=blocked_id, status="POSTED", number="INV-0003"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{draft_id}").mock(
        return_value=Response(200, json=_invoice(id=draft_id, status="DRAFT", number="INV-0004"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{no_email_id}").mock(
        return_value=Response(200, json=_invoice(id=no_email_id, status="POSTED", number="INV-0005"))
    )

    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        side_effect=[
            Response(200, json=_contact(email="a@example.com")),
            Response(200, json=_contact(email="b@example.com")),
            Response(200, json=_contact(email="")),  # no_email row
        ]
    )

    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{sent_id}/send-email").mock(
        return_value=Response(200, json={"mode": "sent", "log_id": "l1", "message_id": "m1"})
    )
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{blocked_id}/send-email").mock(
        return_value=Response(200, json={"mode": "blocked", "log_id": "l2", "reason": "kill switch off"})
    )

    # The 303 redirect target (/invoices) — followed within the same client
    # so the Set-Cookie carrying the session flash rides along, same pattern
    # as test_invoices_transitions_web.py's conflict/validation-error tests.
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            "/invoices/bulk",
            data={
                "action": "send",
                "ids[]": [sent_id, blocked_id, draft_id, no_email_id],
            },
        )

    assert resp.status_code == 200
    body = resp.text
    assert "1 sent" in body
    assert "1 blocked" in body
    assert "2 skipped" in body
    assert "INV-0004" in body  # not posted
    assert "INV-0005" in body  # no customer email


@pytest.mark.anyio
@respx.mock
async def test_bulk_send_never_reports_blocked_as_sent(respx_mock: respx.MockRouter) -> None:
    """send-email returns HTTP 200 even when blocked — the bulk handler must
    read `mode`, not the status code, or a fully-blocked batch would be
    misreported as fully sent."""
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_invoice(status="POSTED"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_contact())
    )
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/send-email").mock(
        return_value=Response(200, json={"mode": "blocked", "log_id": "l3", "reason": "off"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post("/invoices/bulk", data={"action": "send", "ids[]": [_INVOICE_ID]})

    assert resp.status_code == 200
    assert "1 sent" not in resp.text
    assert "1 blocked" in resp.text


# ---------------------------------------------------------------------------
# 9. The removed bulk "mark_paid" action is now rejected, not silently 404ed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bulk_mark_paid_action_removed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            "/invoices/bulk", data={"action": "mark_paid", "ids[]": [_INVOICE_ID]}
        )

    assert resp.status_code == 200
    assert "Unknown bulk action" in resp.text


# ---------------------------------------------------------------------------
# 10. /payments/new prefill from the invoice "Record payment" link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payments_new_prefills_from_query_params(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_contact()]})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/payments/new",
            params={
                "contact_id": _CONTACT_ID,
                "invoice_id": _INVOICE_ID,
                "amount": "110.00",
                "reference": "Invoice INV-0001",
            },
        )

    assert resp.status_code == 200
    assert f'value="{_INVOICE_ID}"' in resp.text
    assert 'value="110.00"' in resp.text
    assert "Invoice INV-0001" in resp.text
