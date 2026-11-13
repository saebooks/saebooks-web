"""b11 "Getting paid without chasing" — web screens.

Covers: recurring invoice send options + failure badge, recurring bills
pages, Getting paid settings/health page, Send reminder proxy, statement
email + bulk statements, credit note email, email-log delivery badges and
filter, and the PUBLIC /pay/{token} page (no login) with its redirect to
Stripe. All engine calls are mocked with respx.
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

_API = settings.api_url.rstrip("/")
_RI = "bbbbbbbb-bbbb-bbbb-bbbb-b11000000001"
_RB = "bbbbbbbb-bbbb-bbbb-bbbb-b11000000002"
_INV = "aaaaaaaa-aaaa-aaaa-aaaa-b11000000001"
_CN = "aaaaaaaa-aaaa-aaaa-aaaa-b11000000002"
_CONTACT = "cccccccc-cccc-cccc-cccc-b11000000001"
_TOKEN = "ab" * 24


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode())).decode()


_SESSION = _cookie({"api_token": "test-token-b11"})


def _client(**kw) -> AsyncClient:  # type: ignore[no-untyped-def]
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION},
        follow_redirects=False,
        **kw,
    )


_RI_BODY = {
    "id": _RI, "company_id": "f" * 8 + "-ffff-ffff-ffff-ffffffffffff",
    "tenant_id": "00000000-0000-0000-0000-000000000001", "contact_id": _CONTACT,
    "name": "Retainer", "frequency": "MONTHLY", "status": "ACTIVE", "anchor_day": 1,
    "next_run": "2026-12-01", "end_date": None, "last_run": None, "due_days": 14,
    "payment_terms": None, "notes": None, "auto_post": True, "invoices_generated": 2,
    "version": 3, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    "archived_at": None, "lines": [], "auto_send": True, "send_to": None, "catch_up": False,
    "last_run_at": "2026-11-01T20:00:00Z", "last_sent_at": None,
    "last_error": "Invoice 1042 was made but not emailed: There is no email address for Acme.",
    "last_error_at": "2026-11-01T20:00:00Z", "failure_count": 2,
}


# --------------------------------------------------------------------------- #
# Recurring invoices: send options + failure badge                             #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_recurring_invoice_detail_shows_failure_and_send_option(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/recurring_invoices/{_RI}").mock(return_value=Response(200, json=_RI_BODY))
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT}").mock(return_value=Response(200, json={"id": _CONTACT, "name": "Acme"}))
    async with _client() as c:
        r = await c.get(f"/recurring-invoices/{_RI}")
    assert r.status_code == 200
    assert "The last run did not finish" in r.text
    assert "not emailed" in r.text
    assert "2 times in a row" in r.text
    assert "Emailed automatically" in r.text
    assert "Make only the latest" in r.text


@pytest.mark.anyio
@respx.mock
async def test_recurring_invoice_create_sends_new_fields(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/recurring_invoices").mock(
        return_value=Response(201, json={**_RI_BODY, "last_error": None})
    )
    async with _client() as c:
        r = await c.post("/recurring-invoices/new", data={
            "name": "Retainer", "contact_id": _CONTACT, "frequency": "MONTHLY",
            "next_run": "2026-12-01", "auto_post": "1", "auto_send": "1",
            "send_to": "a@x.com", "skip_missed": "1",
            "lines[0][account_id]": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "lines[0][description]": "Retainer", "lines[0][quantity]": "1",
            "lines[0][unit_price]": "500",
        })
    assert r.status_code == 303
    sent = _json.loads(route.calls[0].request.content)
    assert sent["auto_send"] is True
    assert sent["send_to"] == "a@x.com"
    assert sent["catch_up"] is False


# --------------------------------------------------------------------------- #
# Recurring bills                                                             #
# --------------------------------------------------------------------------- #

_RB_BODY = {
    "id": _RB, "company_id": "f" * 8 + "-ffff-ffff-ffff-ffffffffffff",
    "tenant_id": "00000000-0000-0000-0000-000000000001", "contact_id": _CONTACT,
    "name": "Workshop rent", "frequency": "MONTHLY", "status": "ACTIVE", "anchor_day": 1,
    "next_run": "2026-12-01", "end_date": None, "last_run": "2026-11-01", "due_days": 7,
    "supplier_reference": "LEASE-1", "notes": None, "auto_post": True, "catch_up": True,
    "bills_generated": 4, "last_run_at": None, "last_error": None, "last_error_at": None,
    "failure_count": 0, "version": 5, "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z", "archived_at": None,
    "lines": [{"id": "e" * 8 + "-eeee-eeee-eeee-eeeeeeeeeeee", "line_no": 1, "description": "Rent",
               "account_id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "tax_code_id": None,
               "quantity": "1.0000", "unit_price": "1500.0000", "discount_pct": "0.00"}],
}


@pytest.mark.anyio
@respx.mock
async def test_recurring_bills_list_and_detail(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/recurring_bills").mock(
        return_value=Response(200, json={"items": [_RB_BODY], "total": 1, "limit": 200, "offset": 0})
    )
    respx_mock.get(f"{_API}/api/v1/recurring_bills/{_RB}").mock(return_value=Response(200, json=_RB_BODY))
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT}").mock(return_value=Response(200, json={"id": _CONTACT, "name": "Landlord Pty Ltd"}))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(return_value=Response(200, json={"items": [
        {"id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "code": "6-1000", "name": "Rent"}]}))
    async with _client() as c:
        r = await c.get("/recurring-bills")
        assert r.status_code == 200
        assert "Workshop rent" in r.text and "Landlord Pty Ltd" in r.text
        r = await c.get(f"/recurring-bills/{_RB}")
    assert r.status_code == 200
    assert "Make next bill now" in r.text
    assert "6-1000 Rent" in r.text


@pytest.mark.anyio
@respx.mock
async def test_recurring_bill_generate_redirects_to_bill(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/recurring_bills/{_RB}/generate").mock(
        return_value=Response(201, json={"bill_id": "99999999-9999-9999-9999-999999999999", "status": "POSTED"})
    )
    async with _client() as c:
        r = await c.post(f"/recurring-bills/{_RB}/generate", data={"version": "5"})
    assert r.status_code == 303
    assert r.headers["location"] == "/bills/99999999-9999-9999-9999-999999999999"


@pytest.mark.anyio
@respx.mock
async def test_recurring_hub_no_longer_says_coming_soon(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/recurring_invoices").mock(return_value=Response(200, json={"items": [_RI_BODY]}))
    respx_mock.get(f"{_API}/api/v1/recurring_bills").mock(return_value=Response(200, json={"items": [_RB_BODY]}))
    respx_mock.get(f"{_API}/api/v1/journal_templates").mock(return_value=Response(200, json={"items": []}))
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/recurring")
    assert r.status_code == 200
    assert "Recurring bills + expenses — coming soon" not in r.text
    assert "/recurring-bills" in r.text
    assert "failed on their last run" in r.text


# --------------------------------------------------------------------------- #
# Getting paid settings + reminders                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_getting_paid_settings_page(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/collections/settings").mock(return_value=Response(200, json={
        "reminders_enabled": True, "reminder_days": [-3, 7, 30], "from_addr": None,
        "effective_from_addr": "accounts@biz.test", "reminder_message": None,
        "online_payment_account_id": None, "online_fee_account_id": None, "online_fee_tax_code_id": None,
    }))
    respx_mock.get(f"{_API}/api/v1/collections/status").mock(return_value=Response(200, json={
        "scheduler": {"enabled": True, "last_finished_at": "2026-11-13T20:00:00+00:00"},
        "recurring_invoice_failures": [{"id": _RI, "name": "Retainer", "last_error": "boom"}],
        "recurring_bill_failures": [], "recurring_invoices_waiting": 0,
        "reminders_enabled": True, "online_payments": {"available": False, "reason": "Online card payment is not switched on."},
    }))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(return_value=Response(200, json={"items": []}))
    respx_mock.get(f"{_API}/api/v1/tax_codes").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/settings/getting-paid")
    assert r.status_code == 200
    assert "-3, 7, 30" in r.text
    assert "accounts@biz.test" in r.text
    assert "These did not run" in r.text and "boom" in r.text
    assert "Online card payment is not switched on." in r.text


@pytest.mark.anyio
@respx.mock
async def test_getting_paid_settings_save(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.put(f"{_API}/api/v1/collections/settings").mock(return_value=Response(200, json={}))
    async with _client() as c:
        r = await c.post("/settings/getting-paid", data={
            "reminders_enabled": "1", "reminder_days": "7, 14", "from_addr": "a@b.test",
        })
    assert r.status_code == 303
    body = _json.loads(route.calls[0].request.content)
    assert body["reminders_enabled"] is True and body["reminder_days"] == "7, 14"
    assert body["online_payment_account_id"] is None


@pytest.mark.anyio
@respx.mock
async def test_send_reminder_proxies_and_flashes(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/send-reminder").mock(
        return_value=Response(200, json={"outcome": "sent", "to": ["c@x.test"], "detail": None})
    )
    async with _client() as c:
        r = await c.post(f"/invoices/{_INV}/send-reminder", data={})
    assert r.status_code == 303
    assert r.headers["location"] == f"/invoices/{_INV}"


@pytest.mark.anyio
@respx.mock
async def test_send_reminder_back_to_list_from_referer(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/send-reminder").mock(
        return_value=Response(422, json={"detail": "Nothing is owing on this invoice"})
    )
    async with _client() as c:
        r = await c.post(f"/invoices/{_INV}/send-reminder", data={},
                         headers={"referer": "http://test/invoices?status=overdue"})
    assert r.status_code == 303
    assert r.headers["location"] == "/invoices?status=overdue"


def test_back_never_redirects_off_site() -> None:
    """The redirect-back helper only ever returns a same-site path (a foreign
    Referer is also refused earlier by CSRF Layer 2)."""
    from types import SimpleNamespace

    from saebooks_web.routes.getting_paid import _back

    def req(ref: str) -> SimpleNamespace:
        return SimpleNamespace(headers={"referer": ref}, url=SimpleNamespace(hostname="test"))

    assert _back(req("https://evil.example/phish"), "/x") == "/x"  # type: ignore[arg-type]
    assert _back(req("http://test/invoices?a=1"), "/x") == "/invoices?a=1"  # type: ignore[arg-type]
    assert _back(req(""), "/x") == "/x"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Statements + credit notes                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_contact_statement_email_uses_period(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/contacts/{_CONTACT}/statement/send-email").mock(
        return_value=Response(200, json={"mode": "sent", "log_id": "x", "reason": None})
    )
    async with _client() as c:
        r = await c.post(f"/contacts/{_CONTACT}/statement/email", data={"from": "2026-07-01", "to": "2026-10-31"})
    assert r.status_code == 303
    body = _json.loads(route.calls[0].request.content)
    assert body == {"from": "2026-07-01", "to": "2026-10-31"}


@pytest.mark.anyio
@respx.mock
async def test_bulk_statements_page_and_send(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/collections/statement-candidates").mock(return_value=Response(200, json={
        "as_of": "2026-10-31",
        "items": [
            {"contact_id": _CONTACT, "name": "Acme", "email": "a@acme.test", "balance": "1200.00", "overdue": "300.00", "email_bounced": False},
            {"contact_id": "c" * 8 + "-cccc-cccc-cccc-000000000002", "name": "No Mail Co", "email": None, "balance": "50.00", "overdue": "0", "email_bounced": False},
        ],
    }))
    send = respx_mock.post(f"{_API}/api/v1/collections/statements/send-bulk").mock(return_value=Response(200, json={
        "counts": {"sent": 1, "drafted": 0, "blocked": 0, "failed": 0, "skipped": 0},
        "results": [{"contact_id": _CONTACT, "name": "Acme", "outcome": "sent"}],
    }))
    async with _client() as c:
        r = await c.get("/statements/send?from=2026-10-01&to=2026-10-31")
        assert r.status_code == 200
        assert "Acme" in r.text and "No email" in r.text
        r = await c.post("/statements/send", data={"from": "2026-10-01", "to": "2026-10-31", "contact_id": _CONTACT})
    assert r.status_code == 303
    body = _json.loads(send.calls[0].request.content)
    assert body == {"period_from": "2026-10-01", "period_to": "2026-10-31", "contact_ids": [_CONTACT]}


@pytest.mark.anyio
@respx.mock
async def test_credit_note_email(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/credit_notes/{_CN}/send-email").mock(
        return_value=Response(200, json={"mode": "sent", "log_id": "x"})
    )
    async with _client() as c:
        r = await c.post(f"/credit-notes/{_CN}/email", data={})
    assert r.status_code == 303
    assert r.headers["location"] == f"/credit-notes/{_CN}"


# --------------------------------------------------------------------------- #
# Email log delivery badges                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_email_log_shows_bounced_not_sent(respx_mock: respx.MockRouter) -> None:
    session = _cookie({"api_token": "t", "user_id": "u1", "user_email": "u@x.test"})
    route = respx_mock.get(f"{_API}/api/v1/email-log").mock(return_value=Response(200, json={
        "items": [{
            "id": "11111111-1111-1111-1111-111111111111", "doc_type": "invoice", "doc_id": _INV,
            "from_addr": "a@b.test", "to_addrs": ["c@d.test"], "subject": "Invoice 1",
            "resend_status": "sent", "delivery_state": "bounced", "bounce_reason": "Mailbox does not exist",
            "kill_switch_reason": None, "resend_error": None, "sent_at": "2026-11-13T01:02:03Z",
        }],
        "total": 1,
    }))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: session}) as c:
        r = await c.get("/email-log?delivery=bounced")
    assert r.status_code == 200
    assert "Bounced" in r.text
    assert "Mailbox does not exist" in r.text
    assert route.calls[0].request.url.params["delivery"] == "bounced"


# --------------------------------------------------------------------------- #
# Public pay page                                                              #
# --------------------------------------------------------------------------- #


def _anon() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False)


@pytest.mark.anyio
@respx.mock
async def test_pay_page_is_public_and_shows_balance(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/pay/{_TOKEN}").mock(return_value=Response(200, json={
        "invoice_number": "1042", "company_name": "Sauer Engineering", "customer_name": "Acme",
        "currency": "AUD", "total": "1100.00", "amount_paid": "100.00", "balance_due": "1000.00",
        "due_date": "2026-11-30", "state": "open", "can_pay_online": True, "unavailable_reason": None,
    }))
    async with _anon() as c:
        r = await c.get(f"/pay/{_TOKEN}")
    assert r.status_code == 200
    assert "$1,000.00" in r.text and "Invoice 1042" in r.text
    assert "Pay by card" in r.text
    assert r.headers.get("cache-control") == "no-store"
    assert 'data-theme="dark"' in r.text


@pytest.mark.anyio
@respx.mock
async def test_pay_page_unknown_token(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/pay/nope").mock(return_value=Response(404, json={"detail": "x"}))
    async with _anon() as c:
        r = await c.get("/pay/nope")
    assert r.status_code == 404
    assert "not valid" in r.text


@pytest.mark.anyio
@respx.mock
async def test_pay_post_redirects_to_stripe(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/pay/{_TOKEN}/checkout").mock(
        return_value=Response(200, json={"url": "https://checkout.stripe.com/c/pay/cs_test_1"})
    )
    async with _client() as c:  # a session cookie carries the CSRF token
        r = await c.post(f"/pay/{_TOKEN}", data={})
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_1"


@pytest.mark.anyio
@respx.mock
async def test_pay_post_paid_invoice_explains(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/pay/{_TOKEN}/checkout").mock(
        return_value=Response(422, json={"detail": "This invoice is already paid. Thank you."})
    )
    async with _client() as c:
        r = await c.post(f"/pay/{_TOKEN}", data={})
    assert r.status_code == 200
    assert "already paid" in r.text


# --------------------------------------------------------------------------- #
# Invoice PDF carries the Pay now link                                         #
# --------------------------------------------------------------------------- #


def test_invoice_pdf_template_has_pay_online_link() -> None:
    from saebooks_web.render import get_env
    from tests.test_internal_render import _invoice_ctx

    url = "https://books.example.test/pay/" + "ab" * 24
    env = get_env()
    ctx = _invoice_ctx()
    ctx["pay_online_url"] = url
    tex = env.get_template("document.tex.j2").render(**ctx)
    assert "Pay online:" in tex
    assert "\\href{" + url + "}{Pay by card}" in tex

    # No bank details on file: a How to Pay panel with just the card link.
    ctx = _invoice_ctx()
    ctx["pay_online_url"] = url
    ctx["company"]["bank"] = {}
    tex = env.get_template("document.tex.j2").render(**ctx)
    assert "Pay online by card:" in tex

    # Without a link nothing changes.
    tex = env.get_template("document.tex.j2").render(**_invoice_ctx())
    assert "Pay online" not in tex
