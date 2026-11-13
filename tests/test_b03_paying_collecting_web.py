"""Batch 3 — paying and collecting screens (web).

Gaps covered: P0-12 (bulk transitions send versions; wrong-state rows are
skipped), P0-13 (remittance view / download / email, no internal notes,
email on post), P0-14 (open-document picker, Pay from bill, Pay selected),
P0-15 (payment terms on contacts, terms-based due dates), P1-2 (names, not
IDs), P1-3 (supplier invoice viewer). The engine is mocked with respx; no
mail is sent.
"""
from __future__ import annotations

import json as _json
from base64 import b64decode as _b64decode
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_SUPPLIER = "22222222-2222-2222-2222-222222222222"
_OTHER_SUPPLIER = "23232323-2323-2323-2323-232323232323"
_BANK = "55555555-5555-5555-5555-555555555555"
_BILL_A = "44444444-4444-4444-4444-44444444444a"
_BILL_B = "44444444-4444-4444-4444-44444444444b"
_PAYMENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_COMPANY = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_ATT = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode())).decode()


_SESSION = _cookie({"api_token": "t-b03", "user_id": "99999999-9999-9999-9999-999999999999"})


def _client(**kw) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION}, **kw,
    )


def _flash(resp) -> str:
    raw = resp.cookies.get(settings.session_cookie_name)
    assert raw, "no session cookie"
    return _json.loads(_b64decode(_TimestampSigner(settings.secret_key).unsign(raw))).get("flash", "")


_OPEN_DOCS = {
    "contact_id": _SUPPLIER, "direction": "OUTGOING",
    "items": [
        {"kind": "bill", "id": _BILL_B, "number": "BILL-000010", "supplier_reference": "INV-1700",
         "issue_date": "2026-04-01", "due_date": "2026-04-30", "overdue": True, "currency": "AUD",
         "total": "55.00", "amount_paid": "0.00", "amount_due": "55.00"},
        {"kind": "bill", "id": _BILL_A, "number": "BILL-000011", "supplier_reference": "INV-1860",
         "issue_date": "2026-05-01", "due_date": "2026-05-31", "overdue": False, "currency": "AUD",
         "total": "110.00", "amount_paid": "22.00", "amount_due": "88.00"},
    ],
}


def _bill(bill_id: str, contact: str = _SUPPLIER, **kw) -> dict:
    base = {"id": bill_id, "number": "BILL-000011", "supplier_reference": "INV-1860",
            "contact_id": contact, "status": "POSTED", "version": 4, "total": "110.00",
            "amount_paid": "22.00", "subtotal": "100.00", "tax_total": "10.00",
            "issue_date": "2026-05-01", "due_date": "2026-05-31", "currency": "AUD",
            "lines": [], "notes": None}
    base.update(kw)
    return base


def _payment(**kw) -> dict:
    base = {"id": _PAYMENT, "company_id": _COMPANY, "number": "PAY-000368",
            "direction": "OUTGOING", "status": "POSTED", "version": 3, "contact_id": _SUPPLIER,
            "bank_account_id": _BANK, "payment_date": "2026-05-20", "amount": "110.00",
            "currency": "AUD", "method": "eft", "reference": "Osko 42",
            "notes": "INTERNAL owner note", "remittance_note": "Thanks Nick",
            "allocations": [{"id": "al-1", "payment_id": _PAYMENT, "bill_id": _BILL_A,
                             "invoice_id": None, "credit_note_id": None, "amount": "88.00"}]}
    base.update(kw)
    return base


def _dropdowns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={
        "items": [{"id": _SUPPLIER, "name": "Webber Refrigeration", "contact_type": "SUPPLIER"}]}))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(return_value=Response(200, json={
        "items": [{"id": _BANK, "code": "1-1110", "name": "Cheque account", "reconcile": True,
                   "is_header": False, "account_type": "ASSET"}], "total": 1}))


# ---------------------------------------------------------------------------
# P0-14 — picker, Pay from bill, Pay selected
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_open_documents_partial_lists_bills_by_number(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/settlement/open-documents").mock(
        return_value=Response(200, json=_OPEN_DOCS))
    async with _client() as c:
        r = await c.get("/payments/_open_documents",
                        params={"contact_id": _SUPPLIER, "direction": "OUT"})
    assert r.status_code == 200
    assert "INV-1700" in r.text and "INV-1860" in r.text
    assert "Pay oldest first" in r.text
    assert f'name="open[0][id]" value="{_BILL_B}"' in r.text
    assert 'data-due="88.00"' in r.text
    assert "Overdue" in r.text


@pytest.mark.anyio
@respx.mock
async def test_pay_from_bill_opens_supplier_payment_with_bill_ticked(
    respx_mock: respx.MockRouter,
) -> None:
    _dropdowns(respx_mock)
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_A}").mock(return_value=Response(200, json=_bill(_BILL_A)))
    respx_mock.get(f"{_API}/api/v1/settlement/open-documents").mock(
        return_value=Response(200, json=_OPEN_DOCS))
    async with _client() as c:
        r = await c.get("/payments/new", params={"bill_id": _BILL_A})
    assert r.status_code == 200
    assert '<option value="OUTGOING" selected>' in r.text
    # The bill is ticked in the picker (not pasted into a number box)...
    assert 'data-open-doc-pick checked' in r.text
    # ...and the payment amount defaults to what is owing on it.
    assert 'value="88.00"' in r.text


@pytest.mark.anyio
@respx.mock
async def test_create_from_picker_sends_ids_and_sums_amount(respx_mock: respx.MockRouter) -> None:
    post = respx_mock.post(f"{_API}/api/v1/payments").mock(
        return_value=Response(201, json=_payment(status="DRAFT")))
    form = {
        "direction": "OUTGOING", "contact_id": _SUPPLIER, "payment_date": "2026-05-20",
        "amount": "", "method": "eft", "bank_account_id": _BANK, "idempotency_key": "k-1",
        "remittance_note": "Thanks Nick", "notes": "internal",
        "open[0][pick]": "1", "open[0][kind]": "bill", "open[0][id]": _BILL_B,
        "open[0][due]": "55.00", "open[0][amount]": "",
        "open[1][pick]": "1", "open[1][kind]": "bill", "open[1][id]": _BILL_A,
        "open[1][due]": "88.00", "open[1][amount]": "40.00",
        "allocations[0][target_type]": "INVOICE", "allocations[0][target_id]": "",
        "allocations[0][amount]": "",
    }
    async with _client(follow_redirects=False) as c:
        r = await c.post("/payments/new", data=form)
    assert r.status_code == 303, r.text
    body = _json.loads(post.calls.last.request.content)
    assert body["amount"] == "95.00"
    assert body["allocations"] == [
        {"bill_id": _BILL_B, "amount": "55.00"},
        {"bill_id": _BILL_A, "amount": "40.00"},
    ]
    assert body["remittance_note"] == "Thanks Nick"


@pytest.mark.anyio
@respx.mock
async def test_unticked_rows_are_not_allocated(respx_mock: respx.MockRouter) -> None:
    post = respx_mock.post(f"{_API}/api/v1/payments").mock(
        return_value=Response(201, json=_payment(status="DRAFT")))
    form = {
        "direction": "OUTGOING", "contact_id": _SUPPLIER, "payment_date": "2026-05-20",
        "amount": "20.00", "method": "eft", "bank_account_id": _BANK, "idempotency_key": "k-2",
        "open[0][kind]": "bill", "open[0][id]": _BILL_B, "open[0][due]": "55.00",
        "open[0][amount]": "",
    }
    async with _client(follow_redirects=False) as c:
        await c.post("/payments/new", data=form)
    assert _json.loads(post.calls.last.request.content)["allocations"] == []


@pytest.mark.anyio
@respx.mock
async def test_pay_selected_bills_redirects_to_payment(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_A}").mock(return_value=Response(200, json=_bill(_BILL_A)))
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_B}").mock(return_value=Response(200, json=_bill(_BILL_B)))
    async with _client(follow_redirects=False) as c:
        r = await c.post("/bills/bulk", data={"action": "pay", "ids[]": [_BILL_A, _BILL_B]})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/payments/new?")
    assert "direction=OUTGOING" in loc and f"contact_id={_SUPPLIER}" in loc
    assert _BILL_A in loc and _BILL_B in loc


@pytest.mark.anyio
@respx.mock
async def test_pay_selected_refuses_two_suppliers(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_A}").mock(return_value=Response(200, json=_bill(_BILL_A)))
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_B}").mock(
        return_value=Response(200, json=_bill(_BILL_B, contact=_OTHER_SUPPLIER)))
    async with _client(follow_redirects=False) as c:
        r = await c.post("/bills/bulk", data={"action": "pay", "ids[]": [_BILL_A, _BILL_B]})
    assert r.headers["location"] == "/bills"
    assert "more than one supplier" in _flash(r)


# ---------------------------------------------------------------------------
# P0-12 — bulk transitions on every list send versions; wrong state skipped
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bulk_post_bills_sends_version_and_key(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_A}").mock(
        return_value=Response(200, json=_bill(_BILL_A, status="DRAFT", version=7)))
    post = respx_mock.post(f"{_API}/api/v1/bills/{_BILL_A}/post").mock(
        return_value=Response(200, json=_bill(_BILL_A)))
    async with _client(follow_redirects=False) as c:
        r = await c.post("/bills/bulk", data={"action": "post", "ids[]": [_BILL_A]})
    assert r.status_code == 303
    req = post.calls.last.request
    assert req.headers["If-Match"] == "7"
    assert req.headers.get("X-Idempotency-Key")
    assert "1 bill processed" in _flash(r)


@pytest.mark.anyio
@respx.mock
async def test_bulk_archive_never_reaches_a_posted_bill(respx_mock: respx.MockRouter) -> None:
    """DELETE on a POSTED bill voids it in the engine — bulk Archive must
    skip posted rows with a reason instead."""
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_A}").mock(return_value=Response(200, json=_bill(_BILL_A)))
    delete = respx_mock.delete(f"{_API}/api/v1/bills/{_BILL_A}").mock(return_value=Response(204))
    async with _client(follow_redirects=False) as c:
        r = await c.post("/bills/bulk", data={"action": "archive", "ids[]": [_BILL_A]})
    assert not delete.called
    assert "BILL-000011" in _flash(r) and "only drafts" in _flash(r)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "list_url,api_path,action,method,status",
    [
        ("/invoices/bulk", "/api/v1/invoices/{id}/void", "void", "POST", "POSTED"),
        ("/credit-notes/bulk", "/api/v1/credit_notes/{id}/post", "post", "POST", "DRAFT"),
        ("/expenses/bulk", "/api/v1/expenses/{id}", "archive", "DELETE", "DRAFT"),
        ("/quotes/bulk", "/api/v1/quotes/{id}/accept", "accept", "POST", "SENT"),
    ],
)
@respx.mock
async def test_bulk_on_other_lists_sends_if_match(
    respx_mock: respx.MockRouter, list_url: str, api_path: str, action: str, method: str,
    status: str,
) -> None:
    doc_id = "12121212-1212-1212-1212-121212121212"
    get_path = api_path.format(id=doc_id)
    get_path = get_path[: get_path.index(doc_id) + len(doc_id)]
    respx_mock.get(f"{_API}{get_path}").mock(return_value=Response(200, json={
        "id": doc_id, "number": "DOC-1", "status": status, "version": 5}))
    route = respx_mock.request(method, f"{_API}{api_path.format(id=doc_id)}").mock(
        return_value=Response(200 if method == "POST" else 204, json={} if method == "POST" else None))
    async with _client(follow_redirects=False) as c:
        r = await c.post(list_url, data={"action": action, "ids[]": [doc_id]})
    assert r.status_code == 303
    assert route.called, _flash(r)
    assert route.calls.last.request.headers["If-Match"] == "5"


# ---------------------------------------------------------------------------
# P1-2 / P0-13 — payment page: names, remittance actions, notes labelled
# ---------------------------------------------------------------------------


def _detail_mocks(respx_mock: respx.MockRouter, payment: dict) -> None:
    respx_mock.get(f"{_API}/api/v1/payments/{_PAYMENT}").mock(return_value=Response(200, json=payment))
    respx_mock.get(f"{_API}/api/v1/attachments").mock(side_effect=lambda req: Response(
        200, json=([{"id": _ATT, "filename": "inv-1860.pdf", "content_type": "application/pdf",
                     "size": 100, "archived_at": None}]
                   if req.url.params.get("entity_kind") == "bill" else [])))
    respx_mock.get(f"{_API}/api/v1/settlement/payments/{_PAYMENT}").mock(return_value=Response(200, json={
        "payment_id": _PAYMENT, "contact": {"id": _SUPPLIER, "name": "Webber Refrigeration"},
        "amount": "110.00", "allocated": "88.00", "unallocated": "22.00",
        "allocations": [{"allocation_id": "al-1", "kind": "bill", "document_id": _BILL_A,
                         "number": "BILL-000011", "supplier_reference": "INV-1860",
                         "amount": "88.00", "amount_due": "0.00",
                         "credits": [{"reference": "CR-77", "number": "SCN-1", "amount": "22.00"}]}]}))


@pytest.mark.anyio
@respx.mock
async def test_payment_detail_shows_names_and_remittance_actions(respx_mock: respx.MockRouter) -> None:
    _detail_mocks(respx_mock, _payment())
    async with _client() as c:
        r = await c.get(f"/payments/{_PAYMENT}")
    assert r.status_code == 200
    t = r.text
    assert "Webber Refrigeration" in t
    assert "INV-1860" in t and "BILL-000011" in t
    assert f">{_BILL_A}<" not in t  # no raw id as link text
    assert f"/payments/{_PAYMENT}/remittance.pdf" in t
    assert f"/payments/{_PAYMENT}/remittance/email" in t
    assert "Message to the supplier" in t and "Thanks Nick" in t
    assert "never printed on the remittance" in t
    assert f"/attachments/{_ATT}/view" in t  # supplier's invoice beside the bill
    assert "less credit" in t and "CR-77" in t
    assert "Correct details" in t


@pytest.mark.anyio
@respx.mock
async def test_draft_outgoing_payment_offers_email_on_post(respx_mock: respx.MockRouter) -> None:
    _detail_mocks(respx_mock, _payment(status="DRAFT"))
    async with _client() as c:
        r = await c.get(f"/payments/{_PAYMENT}")
    assert 'name="email_remittance"' in r.text
    assert "/remittance.pdf" not in r.text  # drafts have no remittance yet


@pytest.mark.anyio
@respx.mock
async def test_remittance_pdf_proxy_inline_and_download(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payments/{_PAYMENT}/remittance.pdf").mock(return_value=Response(
        200, content=b"%PDF-1.4", headers={"content-type": "application/pdf",
                                           "content-disposition": 'inline; filename="remittance-PAY-000368.pdf"'}))
    async with _client() as c:
        inline = await c.get(f"/payments/{_PAYMENT}/remittance.pdf")
        dl = await c.get(f"/payments/{_PAYMENT}/remittance.pdf?download=1")
    assert inline.content == b"%PDF-1.4"
    assert inline.headers["content-disposition"].startswith("inline")
    assert dl.headers["content-disposition"].startswith("attachment")


@pytest.mark.anyio
@respx.mock
async def test_remittance_pdf_refusal_is_shown(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payments/{_PAYMENT}/remittance.pdf").mock(return_value=Response(
        422, json={"detail": "Remittance advice is available once the payment is posted"}))
    async with _client(follow_redirects=False) as c:
        r = await c.get(f"/payments/{_PAYMENT}/remittance.pdf")
    assert r.status_code == 303
    assert "once the payment is posted" in _flash(r)


def _email_mocks(respx_mock: respx.MockRouter, supplier_email: str | None = "nick@example.com") -> None:
    respx_mock.get(f"{_API}/api/v1/payments/{_PAYMENT}").mock(return_value=Response(200, json=_payment()))
    respx_mock.get(f"{_API}/api/v1/contacts/{_SUPPLIER}").mock(return_value=Response(200, json={
        "id": _SUPPLIER, "name": "Webber Refrigeration", "email": supplier_email}))
    respx_mock.get(f"{_API}/api/v1/companies/{_COMPANY}").mock(return_value=Response(200, json={
        "id": _COMPANY, "name": "Test Co", "legal_name": "Test Co Pty Ltd", "email": "accounts@test.example"}))
    respx_mock.get(f"{_API}/api/v1/settlement/payments/{_PAYMENT}").mock(return_value=Response(200, json={
        "contact": {"name": "Webber Refrigeration"},
        "allocations": [{"kind": "bill", "supplier_reference": "INV-1860", "number": "BILL-000011"}]}))


@pytest.mark.anyio
@respx.mock
async def test_remittance_email_compose_prefills_supplier(respx_mock: respx.MockRouter) -> None:
    _email_mocks(respx_mock)
    async with _client() as c:
        r = await c.get(f"/payments/{_PAYMENT}/remittance/email")
    assert r.status_code == 200
    assert 'value="nick@example.com"' in r.text
    assert "INV-1860" in r.text
    assert "INTERNAL owner note" not in r.text


@pytest.mark.anyio
@respx.mock
async def test_remittance_email_send_reports_blocked_honestly(respx_mock: respx.MockRouter) -> None:
    _email_mocks(respx_mock)
    send = respx_mock.post(f"{_API}/api/v1/payments/{_PAYMENT}/remittance/send-email").mock(
        return_value=Response(200, json={"mode": "blocked", "reason": "kill switch"}))
    async with _client(follow_redirects=False) as c:
        r = await c.post(f"/payments/{_PAYMENT}/remittance/email", data={
            "from_addr": "accounts@test.example", "to": "nick@example.com, ap@example.com",
            "subject": "Remittance", "body_html": "<p>Paid</p>"})
    assert r.status_code == 303
    sent = _json.loads(send.calls.last.request.content)
    assert sent["to"] == ["nick@example.com", "ap@example.com"]
    assert "Not sent" in _flash(r) and "emailed" not in _flash(r)


@pytest.mark.anyio
@respx.mock
async def test_post_with_email_remittance_sends_after_posting(respx_mock: respx.MockRouter) -> None:
    _email_mocks(respx_mock)
    respx_mock.post(f"{_API}/api/v1/payments/{_PAYMENT}/post").mock(
        return_value=Response(200, json=_payment()))
    send = respx_mock.post(f"{_API}/api/v1/payments/{_PAYMENT}/remittance/send-email").mock(
        return_value=Response(200, json={"mode": "sent", "message_id": "m1"}))
    async with _client(follow_redirects=False) as c:
        r = await c.post(f"/payments/{_PAYMENT}/post", data={"version": "2", "email_remittance": "1"})
    assert r.status_code == 303
    assert send.called
    assert _json.loads(send.calls.last.request.content)["to"] == ["nick@example.com"]
    assert "Payment posted." in _flash(r) and "emailed" in _flash(r)


@pytest.mark.anyio
@respx.mock
async def test_post_with_email_but_no_supplier_email_says_so(respx_mock: respx.MockRouter) -> None:
    _email_mocks(respx_mock, supplier_email=None)
    respx_mock.post(f"{_API}/api/v1/payments/{_PAYMENT}/post").mock(
        return_value=Response(200, json=_payment()))
    send = respx_mock.post(f"{_API}/api/v1/payments/{_PAYMENT}/remittance/send-email")
    async with _client(follow_redirects=False) as c:
        r = await c.post(f"/payments/{_PAYMENT}/post", data={"version": "2", "email_remittance": "1"})
    assert not send.called
    assert "no email address" in _flash(r)


# ---------------------------------------------------------------------------
# P1-2 / P1-3 / P0-14 — bill page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_detail_names_balance_pay_and_viewer(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bills/{_BILL_A}").mock(return_value=Response(200, json=_bill(_BILL_A)))
    respx_mock.get(f"{_API}/api/v1/attachments").mock(return_value=Response(200, json=[
        {"id": _ATT, "filename": "inv-1860.pdf", "content_type": "application/pdf", "size": 10,
         "archived_at": None}]))
    respx_mock.get(f"{_API}/api/v1/settlement/bills/{_BILL_A}").mock(return_value=Response(200, json={
        "contact": {"id": _SUPPLIER, "name": "Webber Refrigeration"}, "amount_due": "88.00",
        "payments": [], "credits": [{"id": "s1", "number": "SCN-1", "reference": "CR-77",
                                     "date": "2026-05-02", "amount": "22.00"}]}))
    async with _client() as c:
        r = await c.get(f"/bills/{_BILL_A}")
    t = r.text
    assert r.status_code == 200
    assert "Webber Refrigeration" in t and "INV-1860" in t
    assert "Balance due" in t and "88.00" in t
    assert f"/payments/new?direction=OUTGOING&amp;bill_id={_BILL_A}" in t
    assert f'/attachments/{_ATT}/view' in t and "View invoice" in t
    assert "Payments and credits" in t and "CR-77" in t
    assert f"supplier · <span class=\"ink-2\">{_SUPPLIER}" not in t


@pytest.mark.anyio
@respx.mock
async def test_attachment_view_is_inline(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/attachments/{_ATT}").mock(return_value=Response(200, json={
        "id": _ATT, "filename": "inv-1860.pdf", "content_type": "application/pdf"}))
    respx_mock.get(f"{_API}/api/v1/attachments/{_ATT}/download").mock(
        return_value=Response(200, content=b"%PDF"))
    async with _client() as c:
        r = await c.get(f"/attachments/{_ATT}/view")
    assert r.headers["content-disposition"].startswith("inline")
    assert r.content == b"%PDF"


# ---------------------------------------------------------------------------
# P0-15 — terms on contacts, due date from terms
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_sends_payment_terms(respx_mock: respx.MockRouter) -> None:
    post = respx_mock.post(f"{_API}/api/v1/contacts").mock(
        return_value=Response(201, json={"id": _SUPPLIER}))
    async with _client(follow_redirects=False) as c:
        r = await c.post("/contacts/new", data={
            "name": "Webber", "contact_type": "SUPPLIER",
            "payment_terms_basis": "EOM", "payment_terms_days": "30"})
    assert r.status_code == 303
    body = _json.loads(post.calls.last.request.content)
    assert body["payment_terms_basis"] == "EOM" and body["payment_terms_days"] == 30


@pytest.mark.anyio
@respx.mock
async def test_contact_edit_blank_terms_clears_them(respx_mock: respx.MockRouter) -> None:
    patch = respx_mock.patch(f"{_API}/api/v1/contacts/{_SUPPLIER}").mock(
        return_value=Response(200, json={"id": _SUPPLIER}))
    async with _client(follow_redirects=False) as c:
        await c.post(f"/contacts/{_SUPPLIER}/edit", data={
            "version": "2", "name": "Webber", "contact_type": "SUPPLIER",
            "payment_terms_basis": "", "payment_terms_days": "30"})
    body = _json.loads(patch.calls.last.request.content)
    assert body["payment_terms_basis"] is None and body["payment_terms_days"] is None


@pytest.mark.anyio
@respx.mock
async def test_contact_terms_need_days(respx_mock: respx.MockRouter) -> None:
    post = respx_mock.post(f"{_API}/api/v1/contacts")
    respx_mock.get(url__regex=rf"{_API}/api/v1/(accounts|tax_codes).*").mock(
        return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.post("/contacts/new", data={
            "name": "Webber", "contact_type": "SUPPLIER", "payment_terms_basis": "DAYS",
            "payment_terms_days": ""})
    assert r.status_code == 422
    assert "Enter the number of days" in r.text
    assert not post.called


@pytest.mark.anyio
@respx.mock
async def test_terms_due_date_endpoint(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/settlement/due-date").mock(return_value=Response(200, json={
        "due_date": "2026-06-30", "terms_text": "Due 30 days after the end of the month"}))
    async with _client() as c:
        r = await c.get("/terms/due-date", params={"contact_id": _SUPPLIER, "issue_date": "2026-05-14"})
    assert r.json() == {"due_date": "2026-06-30",
                        "terms_text": "Due 30 days after the end of the month"}


@pytest.mark.anyio
@respx.mock
async def test_new_bill_due_date_not_a_flat_30_days(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"{_API}/api/v1/.*").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get("/bills/new")
    assert r.status_code == 200
    assert 'id="due_date" name="due_date" value=""' in r.text
    assert "/terms/due-date" in r.text


@pytest.mark.anyio
@respx.mock
async def test_new_payment_defaults_to_last_bank_used(respx_mock: respx.MockRouter) -> None:
    other = "56565656-5656-5656-5656-565656565656"
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": []}))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(return_value=Response(200, json={
        "items": [
            {"id": other, "code": "1-1100", "name": "Card", "reconcile": True, "is_header": False},
            {"id": _BANK, "code": "1-1110", "name": "Cheque account", "reconcile": True,
             "is_header": False},
        ], "total": 2}))
    cookie = _cookie({"api_token": "t-b03", "last_bank_account_id": _BANK})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: cookie}) as c:
        r = await c.get("/payments/new?direction=OUTGOING")
    import re
    selected = re.findall(r'<option value="([0-9a-f-]{36})" selected>', r.text)
    assert selected == [_BANK]
