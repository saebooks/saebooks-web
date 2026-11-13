"""Credit applications in the web app (engine migration 0227).

Supplier credit note page: total / applied / remaining, applications list,
"apply to bills" form prefilled oldest first, apply + un-apply posts.
Customer credit note page: the same against invoices. Bill page: credits
applied. Contact page: engine ledger history with running balance and a
real owing / credit card (and the fallback when the engine is older).
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from decimal import Decimal

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web import credit_apply
from saebooks_web.config import settings
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_SCN = "11111111-1111-1111-1111-111111111111"
_CONTACT = "33333333-3333-3333-3333-333333333333"
_B1 = "a0000000-0000-0000-0000-000000000001"
_B2 = "a0000000-0000-0000-0000-000000000002"
_B3 = "a0000000-0000-0000-0000-000000000003"
_APP = "c0000000-0000-0000-0000-000000000001"
_CN = "d0000000-0000-0000-0000-000000000001"
_INV = "e0000000-0000-0000-0000-000000000001"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "test-token"}).encode())
    return signer.sign(payload).decode()


def _client(**kw) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()},
        **kw,
    )


_SCN_POSTED = {
    "id": _SCN, "company_id": "66666666-6666-6666-6666-666666666666",
    "contact_id": _CONTACT, "number": "SCN-000003", "issue_date": "2026-09-14",
    "status": "POSTED", "original_bill_id": None, "supplier_reference": "CR42-1",
    "subtotal": "1513.59", "tax_total": "0", "total": "1513.59",
    "amount_applied": "85.84", "amount_remaining": "1427.75",
    "reason": None, "notes": None, "journal_entry_id": None,
    "void_journal_entry_id": None, "version": 4,
    "created_at": "2026-09-14T00:00:00Z", "updated_at": "2026-09-14T00:00:00Z",
    "lines": [],
    "applications": [{
        "id": _APP, "side": "supplier", "contact_id": _CONTACT,
        "supplier_credit_note_id": _SCN, "bill_id": _B1, "amount": "85.84",
        "applied_date": "2026-11-13", "source": "MANUAL", "created_by": "api",
        "credit_number": "SCN-000003", "document_number": "BILL-000303",
        "document_reference": "CR42-0000147909",
    }],
}


def _bill(bid: str, number: str, issued: str, total: str, paid: str = "0") -> dict:
    return {"id": bid, "number": number, "issue_date": issued, "due_date": issued,
            "status": "POSTED", "total": total, "amount_paid": paid,
            "supplier_reference": f"REF-{number}"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_open_documents_oldest_first_and_spends_remaining() -> None:
    items = [
        _bill(_B2, "BILL-2", "2026-10-21", "156.78"),
        _bill(_B1, "BILL-1", "2026-09-14", "1038.05", paid="85.84"),
        _bill(_B3, "BILL-3", "2026-10-23", "61.51"),
        {**_bill("f" * 8 + "-0000-0000-0000-000000000000", "PAID", "2026-01-01", "10", "10")},
        {**_bill("e" * 8 + "-0000-0000-0000-000000000000", "DRAFT", "2026-01-01", "10"),
         "status": "DRAFT"},
    ]
    docs = credit_apply.open_documents(credit_apply.SUPPLIER, items, Decimal("1100.00"))
    assert [d["number"] for d in docs] == ["BILL-1", "BILL-2", "BILL-3"]
    assert [d["suggested"] for d in docs] == [Decimal("952.21"), Decimal("147.79"), Decimal("0.00")]
    assert docs[0]["reference"] == "REF-BILL-1"


def test_parse_form_reads_amounts_and_rejects_bad_input() -> None:
    allocs, errors = credit_apply.parse_form(
        credit_apply.SUPPLIER,
        {f"amount__{_B1}": "952.21", f"amount__{_B2}": "", f"amount__{_B3}": "0",
         "version": "4"},
    )
    assert allocs == [{"bill_id": _B1, "amount": "952.21"}] and errors == []
    _, errors = credit_apply.parse_form(credit_apply.SUPPLIER, {f"amount__{_B1}": "1.005"})
    assert errors and "decimal places" in errors[0]
    _, errors = credit_apply.parse_form(credit_apply.SUPPLIER, {f"amount__{_B1}": "abc"})
    assert errors


# ---------------------------------------------------------------------------
# Supplier credit note page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_scn_detail_shows_applications_and_prefilled_form(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/supplier_credit_notes/{_SCN}").mock(
        return_value=Response(200, json=_SCN_POSTED))
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT}").mock(
        return_value=Response(200, json={"id": _CONTACT, "name": "Motion Australia Pty Ltd"}))
    bills = respx_mock.get(url__regex=rf"^{_API}/api/v1/bills(\?.*)?$").mock(
        return_value=Response(200, json={"items": [
            _bill(_B2, "BILL-000304", "2026-09-15", "21.52"),
            _bill(_B1, "BILL-000303", "2026-09-14", "1038.05", paid="85.84"),
        ], "total": 2}))
    async with _client() as c:
        r = await c.get(f"/supplier-credit-notes/{_SCN}")
    assert r.status_code == 200, r.text
    t = r.text
    assert "Motion Australia Pty Ltd" in t
    assert "1427.75" in t.replace(",", "")  # credit remaining
    assert "BILL-000303" in t and "CR42-0000147909" in t  # existing application
    assert f"/supplier-credit-notes/{_SCN}/applications/{_APP}/unapply" in t
    assert f'action="/supplier-credit-notes/{_SCN}/apply"' in t
    assert f'name="amount__{_B1}" value="952.21"' in t  # oldest first, capped at outstanding
    assert f'name="amount__{_B2}" value="21.52"' in t
    assert 'name="csrf_token"' in t or "csrf" in t
    q = bills.calls.last.request.url.params
    assert q["contact_id"] == _CONTACT and q["status"] == "POSTED"


@pytest.mark.anyio
@respx.mock
async def test_scn_apply_posts_allocations_with_if_match_and_key(respx_mock) -> None:
    route = respx_mock.post(f"{_API}/api/v1/supplier_credit_notes/{_SCN}/applications").mock(
        return_value=Response(201, json=_SCN_POSTED))
    async with _client(follow_redirects=False) as c:
        r = await c.post(
            f"/supplier-credit-notes/{_SCN}/apply",
            data={"version": "4", "idem": "k-123", "applied_date": "2026-11-13",
                  f"amount__{_B1}": "952.21", f"amount__{_B2}": "21.52",
                  f"amount__{_B3}": ""},
        )
    assert r.status_code == 303
    assert r.headers["location"] == f"/supplier-credit-notes/{_SCN}"
    req = route.calls.last.request
    assert req.headers["If-Match"] == "4"
    assert req.headers["X-Idempotency-Key"] == "k-123"
    body = _json.loads(req.content)
    assert body["applied_date"] == "2026-11-13"
    assert sorted(body["allocations"], key=lambda a: a["bill_id"]) == [
        {"bill_id": _B1, "amount": "952.21"}, {"bill_id": _B2, "amount": "21.52"}]


@pytest.mark.anyio
@respx.mock
async def test_scn_unapply_calls_delete(respx_mock) -> None:
    route = respx_mock.delete(
        url__regex=rf"^{_API}/api/v1/supplier_credit_notes/{_SCN}/applications/{_APP}(\?.*)?$"
    ).mock(return_value=Response(200, json=_SCN_POSTED))
    async with _client(follow_redirects=False) as c:
        r = await c.post(f"/supplier-credit-notes/{_SCN}/applications/{_APP}/unapply",
                         data={"version": "5"})
    assert r.status_code == 303
    assert route.calls.last.request.headers["If-Match"] == "5"


# ---------------------------------------------------------------------------
# Customer credit note, bill page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_credit_note_detail_offers_open_invoices(respx_mock) -> None:
    cn = {"id": _CN, "company_id": "6" * 8 + "-6666-6666-6666-666666666666",
          "tenant_id": "0" * 8 + "-0000-0000-0000-000000000001", "contact_id": _CONTACT,
          "number": "CN-000020", "issue_date": "2026-10-06", "status": "POSTED",
          "original_invoice_id": None, "subtotal": "100", "tax_total": "0", "total": "100.00",
          "amount_allocated": "0", "amount_applied": "0", "amount_remaining": "100.00",
          "version": 1, "lines": [], "applications": []}
    respx_mock.get(f"{_API}/api/v1/credit_notes/{_CN}").mock(return_value=Response(200, json=cn))
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT}").mock(
        return_value=Response(200, json={"id": _CONTACT, "name": "Customer Co"}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/invoices(\?.*)?$").mock(
        return_value=Response(200, json={"items": [
            {"id": _INV, "number": "INV-000090", "issue_date": "2026-10-01",
             "due_date": "2026-10-31", "status": "POSTED", "total": "70.00", "amount_paid": "0"}
        ], "total": 1}))
    async with _client() as c:
        r = await c.get(f"/credit-notes/{_CN}")
    assert r.status_code == 200, r.text
    assert f'action="/credit-notes/{_CN}/apply"' in r.text
    assert f'name="amount__{_INV}" value="70.00"' in r.text
    assert "Applied to invoices" in r.text


@pytest.mark.anyio
@respx.mock
async def test_bill_detail_lists_credits_applied(respx_mock) -> None:
    bill = {"id": _B1, "company_id": "6" * 8 + "-6666-6666-6666-666666666666",
            "tenant_id": "0" * 8 + "-0000-0000-0000-000000000001", "contact_id": _CONTACT,
            "number": "BILL-000303", "issue_date": "2026-09-14", "due_date": "2026-10-30",
            "status": "POSTED", "subtotal": "1038.05", "tax_total": "0", "total": "1038.05",
            "amount_paid": "1038.05", "currency": "AUD", "fx_rate": "1", "version": 2,
            "created_at": "2026-09-14T00:00:00Z", "updated_at": "2026-11-13T00:00:00Z",
            "lines": [],
            "credits_applied": [
                {**_SCN_POSTED["applications"][0], "credit_reference": "CR42-1"},
                {**_SCN_POSTED["applications"][0], "id": "c" * 8 + "-0000-0000-0000-000000000002",
                 "amount": "952.21", "credit_number": "SCN-000003"},
            ]}
    respx_mock.get(f"{_API}/api/v1/bills/{_B1}").mock(return_value=Response(200, json=bill))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/attachments(\?.*)?$").mock(
        return_value=Response(200, json=[]))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get(f"/bills/{_B1}")
    assert r.status_code == 200, r.text
    assert "Credits applied" in r.text
    assert f"/supplier-credit-notes/{_SCN}" in r.text
    assert "952.21" in r.text


# ---------------------------------------------------------------------------
# Contact page
# ---------------------------------------------------------------------------


_LEDGER = {
    "contact_id": _CONTACT, "contact_name": "Motion Australia Pty Ltd",
    "opening_balance": "0", "closing_balance": "224.58",
    "position": {"receivable_outstanding": "0", "receivable_credit": "0",
                 "payable_outstanding": "0", "payable_credit": "224.58", "net": "224.58"},
    "open_credits": [{"kind": "supplier_credit_note", "id": _SCN, "number": "SCN-000003",
                      "date": "2026-09-14", "total": "1513.59", "remaining": "224.58",
                      "side": "payable"}],
    "rows": [
        {"date": "2026-09-14", "kind": "bill", "id": _B1, "url_kind": "bills",
         "number": "BILL-000303", "reference": "CR42-0000147909", "description": "Bill",
         "status": "POSTED", "amount": "1038.05", "effect": "-1038.05", "balance": "-1038.05"},
        {"date": "2026-09-14", "kind": "supplier_credit_note", "id": _SCN,
         "url_kind": "supplier-credit-notes", "number": "SCN-000003", "reference": None,
         "description": "Supplier credit note", "status": "POSTED", "amount": "1513.59",
         "effect": "1513.59", "balance": "475.54"},
        {"date": "2026-11-13", "kind": "credit_applied", "id": _SCN,
         "url_kind": "supplier-credit-notes", "number": "SCN-000003", "reference": None,
         "description": "SCN-000003 applied to BILL-000303", "status": "APPLIED",
         "amount": "1038.05", "effect": "0", "balance": "475.54",
         "related_id": _B1, "related_kind": "bill", "related_number": "BILL-000303"},
    ],
}


def _mock_contact(respx_mock, *, ledger_status: int = 200) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/contacts/{_CONTACT}(\?.*)?$").mock(
        return_value=Response(200, json={
            "id": _CONTACT, "name": "Motion Australia Pty Ltd", "contact_type": "SUPPLIER",
            "version": 1, "balances": {
                "receivable_unpaid": "0", "receivable_overdue": "0",
                "payable_unpaid": "0", "payable_overdue": "0",
                "receivable_credit": "0", "payable_credit": "224.58",
                "net_position": "224.58"}}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/contacts/{_CONTACT}/transactions(\?.*)?$").mock(
        return_value=Response(ledger_status, json=_LEDGER if ledger_status == 200 else {}))
    _empty = {"items": [], "total": 0}
    for kind in ("invoices", "bills", "payments", "credit_notes", "expenses"):
        respx_mock.get(url__regex=rf"^{_API}/api/v1/{kind}(\?.*)?$").mock(
            return_value=Response(200, json=_empty))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/attachments(\?.*)?$").mock(
        return_value=Response(200, json=[]))


@pytest.mark.anyio
@respx.mock
async def test_contact_shows_supplier_credit_and_running_balance(respx_mock) -> None:
    _mock_contact(respx_mock)
    async with _client() as c:
        r = await c.get(f"/contacts/{_CONTACT}")
    assert r.status_code == 200, r.text
    t = r.text
    assert "owes you" in t and "224.58" in t
    assert "Your credit with them" in t
    assert ">Balance<" in t
    assert "Supplier credit" in t and "Credit applied" in t
    assert "Applied to" in t and f"/bills/{_B1}" in t
    assert "unpaid_balance" not in t


@pytest.mark.anyio
@respx.mock
async def test_contact_falls_back_when_engine_has_no_ledger(respx_mock) -> None:
    _mock_contact(respx_mock, ledger_status=404)
    async with _client() as c:
        r = await c.get(f"/contacts/{_CONTACT}")
    assert r.status_code == 200, r.text
    assert ">Balance<" not in r.text  # no running balance without the ledger
    assert "owes you" in r.text  # position card still works from balances


# ---------------------------------------------------------------------------
# Batch 1/2 additions: payments on account, refunds, lists, dashboard,
# aged tie-out, contacts list, inbox kind
# ---------------------------------------------------------------------------

_PAY = "f0000000-0000-0000-0000-000000000001"


@pytest.mark.anyio
@respx.mock
async def test_payment_apply_panel_lists_open_invoices(respx_mock) -> None:
    respx_mock.get(f"{_API}/api/v1/payments/{_PAY}").mock(return_value=Response(200, json={
        "id": _PAY, "contact_id": _CONTACT, "direction": "INCOMING", "status": "POSTED",
        "currency": "AUD", "amount": "132.00", "amount_unallocated": "132.00",
        "payment_date": "2026-09-07", "version": 3, "allocations": []}))
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT}").mock(
        return_value=Response(200, json={"id": _CONTACT, "name": "Off The Wall"}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/invoices(\?.*)?$").mock(
        return_value=Response(200, json={"items": [
            {"id": _INV, "number": "INV-000091", "issue_date": "2026-11-01",
             "due_date": "2026-11-30", "status": "POSTED", "total": "80.00", "amount_paid": "0"}]}))
    async with _client() as c:
        r = await c.get(f"/payments/{_PAY}/apply-panel")
    assert r.status_code == 200, r.text
    assert "On account" in r.text and "132.00" in r.text
    assert f'action="/payments/{_PAY}/allocate"' in r.text
    assert 'name="direction" value="INCOMING"' in r.text
    assert f'name="amount__{_INV}" value="80.00"' in r.text
    assert f"/refunds/new?source=payment&amp;id={_PAY}" in r.text or f"/refunds/new?source=payment&id={_PAY}" in r.text


@pytest.mark.anyio
@respx.mock
async def test_payment_allocate_and_unallocate_call_engine(respx_mock) -> None:
    post = respx_mock.post(f"{_API}/api/v1/payments/{_PAY}/allocations").mock(
        return_value=Response(200, json={"id": _PAY}))
    delete = respx_mock.delete(url__regex=rf"^{_API}/api/v1/payments/{_PAY}/allocations/{_APP}(\?.*)?$").mock(
        return_value=Response(200, json={"id": _PAY}))
    async with _client(follow_redirects=False) as c:
        r = await c.post(f"/payments/{_PAY}/allocate",
                         data={"version": "3", "direction": "INCOMING", "idem": "k9",
                               f"amount__{_INV}": "80.00"})
        assert r.status_code == 303
        r = await c.post(f"/payments/{_PAY}/unallocate",
                         data={"version": "4", "allocation_id": _APP})
        assert r.status_code == 303
    body = _json.loads(post.calls.last.request.content)
    assert body == {"allocations": [{"invoice_id": _INV, "amount": "80.00"}]}
    assert post.calls.last.request.headers["If-Match"] == "3"
    assert delete.calls.last.request.headers["If-Match"] == "4"


@pytest.mark.anyio
@respx.mock
async def test_refund_of_on_account_receipt_creates_draft_outgoing_payment(respx_mock) -> None:
    receipt = {"id": _PAY, "contact_id": _CONTACT, "direction": "INCOMING", "status": "POSTED",
               "amount": "66.00", "amount_unallocated": "66.00", "number": "PAY-000352"}
    respx_mock.get(f"{_API}/api/v1/payments/{_PAY}").mock(return_value=Response(200, json=receipt))
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT}").mock(
        return_value=Response(200, json={"id": _CONTACT, "name": "Off The Wall"}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/bank_accounts(\?.*)?$").mock(
        return_value=Response(200, json={"items": [{"id": _B3, "code": "1-1114", "name": "Westpac"}]}))
    created = respx_mock.post(f"{_API}/api/v1/payments").mock(
        return_value=Response(201, json={"id": "9" * 8 + "-0000-0000-0000-000000000000"}))
    async with _client(follow_redirects=False) as c:
        r = await c.get(f"/refunds/new?source=payment&id={_PAY}")
        assert r.status_code == 200, r.text
        assert "Refund money this customer paid on account" in r.text
        assert 'value="66.00"' in r.text
        r = await c.post("/refunds/new", data={
            "source": "payment", "source_id": _PAY, "amount": "66.00",
            "bank_account_id": _B3, "payment_date": "2026-11-13", "method": "eft", "idem": "x"})
    assert r.status_code == 303 and r.headers["location"].startswith("/payments/")
    body = _json.loads(created.calls.last.request.content)
    assert body["direction"] == "OUTGOING"
    assert body["allocations"] == [{"refund_of_payment_id": _PAY, "amount": "66.00"}]


@pytest.mark.anyio
@respx.mock
async def test_bills_list_unpaid_uses_engine_status_and_totals(respx_mock) -> None:
    route = respx_mock.get(url__regex=rf"^{_API}/api/v1/bills(\?.*)?$").mock(
        return_value=Response(200, json={
            "items": [{**_bill(_B1, "BILL-000303", "2026-09-14", "1038.05", paid="85.84"),
                       "contact_id": _CONTACT, "currency": "AUD", "amount_due": "952.21"}],
            "total": 1, "limit": 50, "offset": 0,
            "totals": {"count": "1", "total": "1038.05", "amount_paid": "85.84", "amount_due": "952.21"}}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/contacts(\?.*)?$").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get("/bills?status=unpaid")
    assert r.status_code == 200, r.text
    assert route.calls.last.request.url.params["payment_status"] == "unpaid"
    assert "952.21" in r.text and "1 matching" in r.text
    assert "All unpaid" in r.text


@pytest.mark.anyio
@respx.mock
async def test_contacts_list_shows_balances_and_owing_filter(respx_mock) -> None:
    route = respx_mock.get(url__regex=rf"^{_API}/api/v1/contacts(\?.*)?$").mock(
        return_value=Response(200, json={"items": [
            {"id": _CONTACT, "name": "Motion Australia Pty Ltd", "contact_type": "SUPPLIER",
             "balances": {"receivable_unpaid": "0", "receivable_overdue": "0", "payable_unpaid": "0",
                          "payable_overdue": "0", "receivable_credit": "0", "payable_credit": "224.58",
                          "net_position": "224.58"}}], "total": 1}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/contacts/one-off-candidates.*$").mock(
        return_value=Response(200, json={"total": 0}))
    async with _client() as c:
        r = await c.get("/contacts?owing=1")
    assert r.status_code == 200, r.text
    q = route.calls[0].request.url.params
    assert q["include_balances"] == "true" and q["owing"] == "true"
    assert "Owed to us" in r.text and "224.58" in r.text


def test_inbox_offers_supplier_credit_kind() -> None:
    from saebooks_web.routes import inbox

    assert "SUPPLIER_CREDIT_NOTE" in inbox._RECORD_KINDS


_SUMMARY = {
    "as_of_date": "2026-11-13",
    "receivables": {"documents_total": 3869.25, "overdue": 100.0, "credit_on_account": 132.0,
                    "net": 3737.25, "control_account_code": "1-1200", "control_balance": 3737.25,
                    "tie_out_difference": 0.0, "open_contacts": 3},
    "payables": {"documents_total": 5038.50, "overdue": 0.0, "credit_on_account": 1599.43,
                 "net": 3439.07, "control_account_code": "2-1200", "control_balance": 3439.07,
                 "tie_out_difference": 0.0, "open_contacts": 4},
    "bank_accounts": [{"account_id": _B3, "code": "1-1114", "name": "Westpac Business One",
                       "kind": "BANK", "balance": 1213.70}],
    "cash_in_bank": 1213.70,
    "links": {"receivables": "/reports/aged-receivables?as_of_date=2026-11-13",
              "payables": "/reports/aged-payables?as_of_date=2026-11-13"},
}


@pytest.mark.anyio
@respx.mock
async def test_dashboard_uses_engine_summary_and_shows_cash_in_bank(respx_mock) -> None:
    from tests.test_dashboard import _register_mocks

    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/dashboard_summary(\?.*)?$").mock(
        return_value=Response(200, json=_SUMMARY))
    _register_mocks(respx_mock)
    async with _client() as c:
        r = await c.get("/")
    assert r.status_code == 200, r.text
    t = r.text
    assert "Cash in bank" in t and "Westpac Business One" in t
    assert "/reports/aged-payables?as_of_date=2026-11-13" in t
    assert "supplier credit / on account" in t
    assert "out from Trade" not in t  # ties out


@pytest.mark.anyio
@respx.mock
async def test_aged_payables_page_shows_credit_lines_and_tie_out(respx_mock) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/aged_payables(\?.*)?$").mock(
        return_value=Response(200, json={
            "as_of_date": "2026-11-13", "buckets": ["current", "1-30 days"],
            "contacts": [], "totals": {"current": 0.0, "1-30 days": 0.0, "total": 0.0},
            "unapplied_credits": [{"contact_id": _CONTACT, "contact_name": "Motion Australia Pty Ltd",
                                   "kind": "supplier_credit_note", "id": _SCN, "number": "SCN-000003",
                                   "date": "2026-09-14", "bucket": "current", "amount": -224.58}],
            "unapplied_total": -224.58, "net_total": -224.58, "control_account_code": "2-1200",
            "control_balance": -224.58, "tie_out_difference": 0.0}))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/.*$").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/reports/aged-payables")
    assert r.status_code == 200, r.text
    assert "SCN-000003" in r.text and "Agrees" in r.text and "Trade Creditors (2-1200)" in r.text
