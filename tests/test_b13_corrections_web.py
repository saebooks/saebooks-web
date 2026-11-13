"""Batch 13 screens: fix a posted invoice / bill, history panel, approvals,
write-off from the invoice, audit log names and export.

The engine is mocked with respx; each test checks what the screen sends
and what it shows.
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
_INV = "aaaaaaaa-0000-0000-0000-000000000001"
_BILL = "aaaaaaaa-0000-0000-0000-000000000002"
_PAY = "aaaaaaaa-0000-0000-0000-000000000003"
_NEW = "aaaaaaaa-0000-0000-0000-000000000009"
_CUST1 = "cccccccc-0000-0000-0000-000000000001"
_CUST2 = "cccccccc-0000-0000-0000-000000000002"
_ACC1 = "ffffffff-0000-0000-0000-000000000001"
_ACC2 = "ffffffff-0000-0000-0000-000000000002"
_LINE = "eeeeeeee-0000-0000-0000-000000000001"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode("utf-8"))).decode("utf-8")


_ADMIN = {settings.session_cookie_name: _cookie({"api_token": "t", "user_role": "admin"})}
_BOOKKEEPER = {settings.session_cookie_name: _cookie({"api_token": "t", "user_role": "bookkeeper"})}


def _client(cookies: dict | None = None) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies if cookies is not None else _ADMIN,
        follow_redirects=False,
    )


def _invoice(**over) -> dict:
    inv = {
        "id": _INV, "company_id": "d", "tenant_id": "t", "contact_id": _CUST2,
        "number": "INV-0042", "issue_date": "2026-04-01", "due_date": "2026-04-30",
        "status": "POSTED", "subtotal": "600.00", "tax_total": "60.00", "total": "660.00",
        "amount_paid": "0.00", "currency": "AUD", "fx_rate": "1", "notes": None,
        "payment_terms": "Net 30", "version": 3,
        "lines": [{
            "id": _LINE, "line_no": 1, "description": "Fabrication", "account_id": _ACC1,
            "tax_code_id": None, "quantity": "1", "unit_price": "600.00", "discount_pct": "0",
            "line_subtotal": "600.00", "line_tax": "60.00", "line_total": "660.00",
        }],
    }
    inv.update(over)
    return inv


def _mock_fix_lookups(m: respx.MockRouter, *, kind: str = "invoice") -> None:
    m.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": [
        {"id": _CUST1, "name": "Right Customer"}, {"id": _CUST2, "name": "Wrong Customer"},
    ]}))
    m.get(f"{_API}/api/v1/accounts").mock(return_value=Response(200, json={"items": [
        {"id": _ACC1, "code": "4-1000", "name": "Sales"},
        {"id": _ACC2, "code": "4-2000", "name": "Service Income"},
    ]}))
    base = "invoices" if kind == "invoice" else "bills"
    doc_id = _INV if kind == "invoice" else _BILL
    m.get(f"{_API}/api/v1/{base}/{doc_id}/line-accounts").mock(return_value=Response(200, json={
        "lines": [{"line_id": _LINE, "line_no": 1, "description": "Fabrication",
                   "posted_account_id": _ACC1, "current_account_id": _ACC1}],
        "reclassifications": [],
    }))


# ---------------------------------------------------------------------------
# Fix page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fix_page_offers_every_correction(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/invoices/{_INV}").mock(return_value=Response(200, json=_invoice()))
    _mock_fix_lookups(respx_mock)
    async with _client() as c:
        r = await c.get(f"/invoices/{_INV}/fix")
    assert r.status_code == 200, r.text
    t = r.text
    for phrase in (
        "Fix invoice INV-0042", "Correct the details", "Credit it and send a corrected invoice",
        "Void it and start a corrected copy", "Move it to another customer",
        "Write off what is still owing", "Move a line to another account", "Copy to a new draft",
    ):
        assert phrase in t, phrase
    assert "Wrong Customer" in t  # current contact named, not an id
    assert 'name="override_reason"' in t  # admin may change a locked period
    assert 'hx-get="/history/invoice/' + _INV + '"' in t


@pytest.mark.anyio
@respx.mock
async def test_fix_page_hides_override_from_bookkeeper_and_paid_options(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/invoices/{_INV}").mock(
        return_value=Response(200, json=_invoice(amount_paid="660.00"))
    )
    _mock_fix_lookups(respx_mock)
    async with _client(_BOOKKEEPER) as c:
        r = await c.get(f"/invoices/{_INV}/fix")
    assert r.status_code == 200
    assert 'name="override_reason"' not in r.text
    assert "payments or credits are already applied" in r.text
    assert "Write off what is still owing" not in r.text  # nothing owing


@pytest.mark.anyio
@respx.mock
async def test_credit_and_reissue_sends_reason_and_version(respx_mock) -> None:
    route = respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/credit-and-reissue").mock(
        return_value=Response(201, json={"credit_note": {"id": "x", "number": "CN-0009"},
                                         "invoice": {"id": _NEW}})
    )
    async with _client() as c:
        r = await c.post(f"/invoices/{_INV}/credit-and-reissue",
                         data={"version": "3", "reason": "Wrong quantity", "credit_date": "2026-05-01"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/invoices/{_NEW}/edit"
    sent = route.calls.last.request
    assert sent.headers["If-Match"] == "3"
    assert _json.loads(sent.content) == {"reason": "Wrong quantity", "credit_date": "2026-05-01"}


@pytest.mark.anyio
@respx.mock
async def test_void_in_locked_period_shows_plain_message(respx_mock) -> None:
    respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/void").mock(return_value=Response(422, json={
        "code": "period_locked", "needs": "reason",
        "detail": "15 January 2026 is in a locked period (the books are locked up to 31 March 2026).",
    }))
    respx_mock.get(f"{_API}/api/v1/invoices/{_INV}").mock(return_value=Response(200, json=_invoice()))
    _mock_fix_lookups(respx_mock)
    async with _client() as c:
        r = await c.post(f"/invoices/{_INV}/void-with-reason", data={"version": "3", "reason": "Duplicate"})
        assert r.status_code == 303 and r.headers["location"] == f"/invoices/{_INV}/fix"
        page = await c.get(f"/invoices/{_INV}/fix")
    assert "locked up to 31 March 2026" in page.text


@pytest.mark.anyio
@respx.mock
async def test_void_with_reason_passes_override(respx_mock) -> None:
    route = respx_mock.post(f"{_API}/api/v1/bills/{_BILL}/void").mock(return_value=Response(200, json={}))
    async with _client() as c:
        r = await c.post(f"/bills/{_BILL}/void-with-reason",
                         data={"version": "2", "reason": "Entered twice",
                               "override_reason": "Agreed with the accountant"})
    assert r.status_code == 303 and r.headers["location"] == f"/bills/{_BILL}"
    assert _json.loads(route.calls.last.request.content) == {
        "reason": "Entered twice", "override_reason": "Agreed with the accountant"}


@pytest.mark.anyio
@respx.mock
async def test_change_contact_and_change_account(respx_mock) -> None:
    cc = respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/change-contact").mock(
        return_value=Response(200, json=_invoice(contact_id=_CUST1)))
    rc = respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/lines/{_LINE}/reclassify").mock(
        return_value=Response(201, json={"id": "r"}))
    async with _client() as c:
        r1 = await c.post(f"/invoices/{_INV}/change-contact",
                          data={"version": "3", "contact_id": _CUST1, "reason": "Wrong customer"})
        r2 = await c.post(f"/invoices/{_INV}/lines/{_LINE}/change-account",
                          data={"to_account_id": _ACC2, "reason": "Service, not sales"})
    assert r1.status_code == 303 and r1.headers["location"] == f"/invoices/{_INV}"
    assert _json.loads(cc.calls.last.request.content) == {"reason": "Wrong customer", "contact_id": _CUST1}
    assert r2.status_code == 303
    assert _json.loads(rc.calls.last.request.content) == {"reason": "Service, not sales", "to_account_id": _ACC2}


@pytest.mark.anyio
@respx.mock
async def test_write_off_from_the_invoice(respx_mock) -> None:
    route = respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/write-off").mock(
        return_value=Response(200, json=_invoice(status="WRITTEN_OFF")))
    async with _client() as c:
        r = await c.post(f"/invoices/{_INV}/write-off",
                         data={"write_off_date": "2026-11-13", "reason": "Customer in liquidation"})
        no_reason = await c.post(f"/invoices/{_INV}/write-off", data={"write_off_date": "2026-11-13"})
    assert r.status_code == 303 and r.headers["location"] == f"/invoices/{_INV}"
    assert _json.loads(route.calls.last.request.content) == {
        "write_off_date": "2026-11-13", "reason": "Customer in liquidation"}
    assert no_reason.headers["location"] == f"/invoices/{_INV}/fix"
    assert route.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_duplicate_opens_the_new_draft(respx_mock) -> None:
    respx_mock.post(f"{_API}/api/v1/invoices/{_INV}/duplicate").mock(
        return_value=Response(201, json={"id": _NEW}))
    async with _client() as c:
        r = await c.post(f"/invoices/{_INV}/duplicate")
    assert r.status_code == 303 and r.headers["location"] == f"/invoices/{_NEW}/edit"


@pytest.mark.anyio
@respx.mock
async def test_correct_posted_bill_details_and_pay_date(respx_mock) -> None:
    patch = respx_mock.patch(f"{_API}/api/v1/bills/{_BILL}").mock(
        return_value=Response(200, json={"id": _BILL, "version": 5, "planned_pay_date": None}))
    put = respx_mock.put(f"{_API}/api/v1/bills/{_BILL}/planned-pay-date").mock(
        return_value=Response(200, json={"id": _BILL, "version": 6}))
    async with _client() as c:
        r = await c.post(f"/bills/{_BILL}/correct", data={
            "version": "4", "due_date": "2026-06-30", "notes": "Paid by card",
            "supplier_reference": "S-77", "planned_pay_date": "2026-06-26"})
    assert r.status_code == 303 and r.headers["location"] == f"/bills/{_BILL}"
    assert _json.loads(patch.calls.last.request.content) == {
        "due_date": "2026-06-30", "notes": "Paid by card", "supplier_reference": "S-77"}
    assert put.calls.last.request.headers["If-Match"] == "5"


# ---------------------------------------------------------------------------
# History panel
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_history_panel_shows_people_reasons_and_changes(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/history/invoice/{_INV}").mock(return_value=Response(200, json={
        "entity": "invoice", "entity_id": _INV, "events": [
            {"at": "2026-11-12T23:00:00+00:00", "action": "create", "label": "Created",
             "user_name": "Bob Bookkeeper", "reason": None, "changes": []},
            {"at": "2026-11-13T00:05:00+00:00", "action": "invoice.change_contact",
             "label": "Moved to another contact", "user_name": "Alice Admin",
             "reason": "Booked to the wrong customer",
             "changes": [{"field": "contact_id", "label": "Contact",
                          "before": "Wrong Customer", "after": "Right Customer"}]},
        ]}))
    async with _client() as c:
        r = await c.get(f"/history/invoice/{_INV}")
    assert r.status_code == 200
    t = r.text
    assert t.index("Moved to another contact") < t.index("Created")  # newest first
    assert "Alice Admin" in t and "Bob Bookkeeper" in t
    assert "Booked to the wrong customer" in t
    assert "Wrong Customer" in t and "Right Customer" in t
    assert "13 Nov 2026, 10:05 am" in t  # Brisbane time, not UTC


@pytest.mark.anyio
@respx.mock
async def test_history_panel_error_and_unknown_entity(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/history/bill/{_BILL}").mock(return_value=Response(500))
    async with _client() as c:
        r = await c.get(f"/history/bill/{_BILL}")
        bad = await c.get(f"/history/contacts/{_BILL}")
    assert "could not be loaded" in r.text
    assert bad.status_code == 404


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_approval_posts_action(respx_mock) -> None:
    route = respx_mock.post(f"{_API}/api/v1/bills/{_BILL}/approval").mock(
        return_value=Response(200, json={"id": _BILL}))
    async with _client() as c:
        r = await c.post(f"/bills/{_BILL}/approval",
                         data={"version": "2", "action": "approve", "planned_pay_date": "2026-11-20"})
    assert r.status_code == 303 and r.headers["location"] == f"/bills/{_BILL}"
    assert _json.loads(route.calls.last.request.content) == {
        "action": "approve", "planned_pay_date": "2026-11-20"}
    assert route.calls.last.request.headers["If-Match"] == "2"


@pytest.mark.anyio
@respx.mock
async def test_payment_send_back_error_is_flashed(respx_mock) -> None:
    respx_mock.post(f"{_API}/api/v1/payments/{_PAY}/approval").mock(
        return_value=Response(422, json={"detail": "Please say why it is being sent back."}))
    async with _client() as c:
        r = await c.post(f"/payments/{_PAY}/approval", data={"version": "1", "action": "send_back"})
    assert r.status_code == 303 and r.headers["location"] == f"/payments/{_PAY}"


@pytest.mark.anyio
@respx.mock
async def test_approvals_page_groups_and_settings(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/approvals").mock(return_value=Response(200, json={"items": [
        {"kind": "bill", "id": _BILL, "number": None, "reference": "S-1", "contact_id": _CUST1,
         "contact_name": "Steel Supplier", "date": "2026-11-01", "total": "550.00",
         "approval_status": "AWAITING_APPROVAL", "approval_note": "Job 1042",
         "approval_requested_at": None, "planned_pay_date": None, "version": 2},
        {"kind": "bill", "id": _NEW, "number": "BILL-9", "reference": None, "contact_id": _CUST1,
         "contact_name": "Timber Supplier", "date": "2026-11-02", "total": "99.00",
         "approval_status": None, "approval_note": None, "approval_requested_at": None,
         "planned_pay_date": "2026-11-21", "version": 4},
    ]}))
    respx_mock.get(f"{_API}/api/v1/approvals/settings").mock(
        return_value=Response(200, json={"bills_need_approval": True, "payments_need_approval": False}))
    put = respx_mock.put(f"{_API}/api/v1/approvals/settings").mock(
        return_value=Response(200, json={"bills_need_approval": False, "payments_need_approval": True}))
    async with _client() as c:
        r = await c.get("/approvals")
        s = await c.post("/approvals/settings", data={"payments_need_approval": "1"})
    assert r.status_code == 200
    t = r.text
    assert "Steel Supplier" in t and "Job 1042" in t
    assert t.index("Waiting for approval") < t.index("Steel Supplier") < t.index("Bills planned to be paid")
    assert "Timber Supplier" in t and "2026-11-21" in t
    assert 'name="bills_need_approval" value="1" checked' in t
    assert s.status_code == 303
    assert _json.loads(put.calls.last.request.content) == {
        "bills_need_approval": False, "payments_need_approval": True}


# ---------------------------------------------------------------------------
# Detail pages wire it all in
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_detail_has_fix_history_and_approval(respx_mock) -> None:
    draft = {
        "id": _BILL, "company_id": "d", "tenant_id": "t", "contact_id": _CUST1, "number": None,
        "issue_date": "2026-11-01", "due_date": "2026-11-30", "status": "DRAFT",
        "subtotal": "500.00", "tax_total": "50.00", "total": "550.00", "amount_paid": "0.00",
        "currency": "AUD", "fx_rate": "1", "notes": None, "version": 2,
        "approval_status": "AWAITING_APPROVAL", "approval_note": "Job 1042",
        "planned_pay_date": None, "lines": [],
    }
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL}").mock(return_value=Response(200, json=draft))
    respx_mock.get(f"{_API}/api/v1/attachments").mock(return_value=Response(200, json=[]))
    respx_mock.get(url__regex=rf"{_API}/api/v1/.*").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get(f"/bills/{_BILL}")
    assert r.status_code == 200, r.text
    t = r.text
    assert "Waiting for approval" in t and "Job 1042" in t
    assert f'action="/bills/{_BILL}/approval"' in t
    assert f'hx-get="/history/bill/{_BILL}"' in t
    assert "change log · TODO" not in t


@pytest.mark.anyio
@respx.mock
async def test_audit_log_shows_names_and_exports(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/audit-log").mock(return_value=Response(200, json={
        "items": [{"id": "1", "actor_user_id": "u", "actor_name": "Alice Admin",
                   "action": "invoice.change_contact", "table_name": "invoices", "row_id": _INV,
                   "row_snapshot": {}, "reason": "Wrong customer", "at": "2026-11-13T00:05:00+00:00"}],
        "total": 1, "limit": 50, "offset": 0}))
    respx_mock.get(f"{_API}/api/v1/users").mock(return_value=Response(200, json={"items": [
        {"id": "u", "display_name": "Alice Admin"}]}))
    csv = respx_mock.get(f"{_API}/api/v1/audit-log.csv").mock(
        return_value=Response(200, text="at,actor_user_id,action\n", headers={"content-type": "text/csv"}))
    async with _client() as c:
        r = await c.get("/admin/audit", params={"action": "invoice.change_contact"})
        x = await c.get("/admin/audit/export.csv", params={"action": "invoice.change_contact",
                                                            "date_from": "2026-11-01"})
    t = r.text
    assert "Alice Admin" in t and "Invoice moved to another customer" in t
    assert "Wrong customer" in t and f'href="/invoices/{_INV}"' in t
    assert x.status_code == 200 and "attachment" in x.headers["content-disposition"]
    q = dict(csv.calls.last.request.url.params)
    assert q["action"] == "invoice.change_contact" and q["at_from"].startswith("2026-11-01")
