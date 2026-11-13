"""Batch 8 "Finding things" — web side.

Covers the list screens handing search, sort, paging, the "all unpaid"
filter and spreadsheet downloads to the engine; the type-ahead contact
lookup; names fetched for exactly the rows on a page (no 500-contact cap);
and the global search page with every kind and "view all".
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from urllib.parse import parse_qs, urlsplit

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web import list_helpers as lh
from saebooks_web.config import settings
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_SUPPLIER = "11111111-1111-1111-1111-111111111111"
_BILL = "22222222-2222-2222-2222-222222222222"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "tok"}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _client(**kw) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()},
        **kw,
    )


def _qs(request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


_BILL_ROW = {
    "id": _BILL, "contact_id": _SUPPLIER, "number": "BILL-0042",
    "supplier_reference": "SUP-77821", "issue_date": "2026-11-01",
    "due_date": "2026-11-30", "status": "POSTED", "total": "450.00",
    "amount_paid": "0.00", "currency": "AUD", "lines": [],
}
_CONTACT_ROW = {"id": _SUPPLIER, "name": "Bravo Steel", "contact_type": "CONTRACTOR"}


# ---------------------------------------------------------------------------
# list_helpers (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("$1,200.50", "1200.50"), ("88.8", "88.8"), ("", ""), ("abc", ""), (None, "")],
)
def test_clean_amount(raw, expected) -> None:
    assert lh.clean_amount(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [(37, 50), (25, 25), (100, 100), (9999, 500), ("x", 50), (0, 50)])
def test_clamp_page_size(raw, expected) -> None:
    assert lh.clamp_page_size(raw) == expected


def test_search_params_drops_blank_and_junk() -> None:
    assert lh.search_params("  acme ", "x", "$2,000") == {"q": "acme", "amount_max": "2000"}
    assert lh.search_params(None, None, None) == {}


# ---------------------------------------------------------------------------
# Bills: unpaid filter, search, server sort, names by id, CSV
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bills_list_sends_filters_to_engine(respx_mock: respx.MockRouter) -> None:
    bills = respx_mock.get(f"{_API}/api/v1/bills").mock(
        return_value=Response(200, json={"items": [_BILL_ROW], "total": 1})
    )
    contacts = respx_mock.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_CONTACT_ROW], "total": 1})
    )
    async with _client() as c:
        r = await c.get("/bills", params={
            "status": "unpaid", "q": "77821", "amount_min": "$100", "sort": "contact_id",
            "direction": "asc", "limit": "100", "offset": "100",
        })
    assert r.status_code == 200, r.text
    sent = _qs(bills.calls.last.request)
    assert sent["payment_status"] == ["unpaid"]
    assert "status" not in sent
    assert sent["q"] == ["77821"]
    assert sent["amount_min"] == ["100"]
    assert sent["sort"] == ["contact"]  # old key mapped to the engine's
    assert sent["direction"] == ["asc"]
    assert sent["page_size"] == ["100"] and sent["page"] == ["2"]
    # Names for exactly this page's suppliers — no "first 500" sweep.
    for call in contacts.calls:
        q = _qs(call.request)
        assert q.get("id") == [_SUPPLIER]
        assert "type" not in q
    body = r.text
    assert "SUP-77821" in body  # supplier invoice number column
    assert "Supplier invoice number" in body
    assert "Bravo Steel" in body  # a contractor's name still shows
    assert "All unpaid" in body
    assert "Download spreadsheet" in body
    assert "export=csv" in body and "q=77821" in body
    assert "data-contact-picker" in body


@pytest.mark.anyio
@respx.mock
async def test_bills_draft_filter_uses_document_status(respx_mock: respx.MockRouter) -> None:
    bills = respx_mock.get(f"{_API}/api/v1/bills").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        await c.get("/bills", params={"status": "draft"})
    sent = _qs(bills.calls.last.request)
    assert sent["status"] == ["DRAFT"]
    assert "payment_status" not in sent


@pytest.mark.anyio
@respx.mock
async def test_bills_export_passes_csv_through(respx_mock: respx.MockRouter) -> None:
    csv_body = "﻿Number,Supplier invoice number\r\nBILL-0042,SUP-77821\r\n"
    bills = respx_mock.get(f"{_API}/api/v1/bills").mock(
        return_value=Response(
            200, text=csv_body,
            headers={"content-type": "text/csv; charset=utf-8",
                     "content-disposition": 'attachment; filename="bills-2026-11-13.csv"'},
        )
    )
    async with _client() as c:
        r = await c.get("/bills", params={"export": "csv", "status": "overdue", "q": "SUP"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "bills-2026-11-13.csv" in r.headers["content-disposition"]
    assert "SUP-77821" in r.text
    sent = _qs(bills.calls.last.request)
    assert sent["format"] == ["csv"]
    assert sent["payment_status"] == ["overdue"]
    assert sent["q"] == ["SUP"]


@pytest.mark.anyio
@respx.mock
async def test_export_refused_redirects_with_plain_reason(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/invoices").mock(
        return_value=Response(422, json={"detail": "There are 60000 matching rows. Narrow the dates."})
    )
    async with _client(follow_redirects=False) as c:
        r = await c.get("/invoices", params={"export": "csv"})
    assert r.status_code == 303
    assert r.headers["location"] == "/invoices"


@pytest.mark.anyio
@respx.mock
async def test_invoices_unpaid_and_contact_name(respx_mock: respx.MockRouter) -> None:
    inv = respx_mock.get(f"{_API}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [{"id": _SUPPLIER, "name": "Alpha Widgets", "contact_type": "CUSTOMER"}]})
    )
    async with _client() as c:
        r = await c.get("/invoices", params={"status": "unpaid", "contact_id": _SUPPLIER, "limit": "37"})
    sent = _qs(inv.calls.last.request)
    assert sent["payment_status"] == ["unpaid"]
    assert sent["contact_id"] == [_SUPPLIER]
    assert sent["page_size"] == ["50"]  # snapped to an offered page size
    # The filter shows the customer's NAME, not the id.
    assert 'value="Alpha Widgets"' in r.text


# ---------------------------------------------------------------------------
# Payments: sort order is sort_direction; direction stays money in/out
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payments_sort_direction_kept_apart(respx_mock: respx.MockRouter) -> None:
    pay = respx_mock.get(f"{_API}/api/v1/payments").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/payments", params={
            "direction": "OUTGOING", "sort": "amount", "sort_direction": "asc", "q": "EFT-1",
        })
    assert r.status_code == 200
    sent = _qs(pay.calls.last.request)
    assert sent["direction"] == ["OUTGOING"]
    assert sent["sort"] == ["amount"]
    assert sent["sort_direction"] == ["asc"]
    assert sent["q"] == ["EFT-1"]
    assert "sort_direction=" in r.text  # sort links use the right parameter
    assert 'data-role="supplier"' in r.text  # picker follows money out


# ---------------------------------------------------------------------------
# Every other list: search forwarded, download works
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("page", "api"),
    [
        ("/receipts", "/api/v1/receipts"),
        ("/credit-notes", "/api/v1/credit_notes"),
        ("/supplier-credit-notes", "/api/v1/supplier_credit_notes"),
        ("/expenses", "/api/v1/expenses"),
        ("/quotes", "/api/v1/quotes"),
        ("/purchase_orders", "/api/v1/purchase_orders"),
        ("/bank-statement-lines", "/api/v1/bank_statement_lines"),
    ],
)
@respx.mock
async def test_other_lists_search_sort_and_download(respx_mock: respx.MockRouter, page: str, api: str) -> None:
    route = respx_mock.get(f"{_API}{api}").mock(
        side_effect=lambda req: (
            Response(200, text="Number\r\n", headers={"content-type": "text/csv; charset=utf-8"})
            if "format=csv" in str(req.url)
            else Response(200, json={"items": [], "total": 0})
        )
    )
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": []}))
    respx_mock.get(f"{_API}/api/v1/bank_accounts").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get(page, params={"q": "needle", "amount_max": "500", "sort": "total", "direction": "asc"})
        assert r.status_code == 200, r.text
        sent = _qs(route.calls.last.request)
        assert sent["q"] == ["needle"]
        assert sent["amount_max"] == ["500"]
        assert "sort" in sent
        assert "Download spreadsheet" in r.text
        assert 'name="q"' in r.text
        d = await c.get(page, params={"export": "csv", "q": "needle"})
        assert d.status_code == 200
        assert d.headers["content-type"].startswith("text/csv")
        assert _qs(route.calls.last.request)["format"] == ["csv"]


# ---------------------------------------------------------------------------
# Contacts: search, sort, download, type-ahead lookup
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contacts_list_search_sort(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_CONTACT_ROW], "total": 1})
    )
    async with _client() as c:
        r = await c.get("/contacts", params={"q": "bravo", "sort": "city", "direction": "desc", "contact_type": "CONTRACTOR"})
    assert r.status_code == 200
    sent = _qs(route.calls[0].request)
    assert sent["q"] == ["bravo"]
    assert sent["sort"] == ["city"]
    assert sent["direction"] == ["desc"]
    assert sent["type"] == ["CONTRACTOR"]
    assert "Bravo Steel" in r.text
    assert 'name="q"' in r.text and 'value="bravo"' in r.text
    assert "Download spreadsheet" in r.text


@pytest.mark.anyio
@respx.mock
async def test_contacts_lookup_json(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [_CONTACT_ROW], "total": 1})
    )
    async with _client() as c:
        r = await c.get("/contacts/_lookup", params={"q": "bra", "role": "supplier"})
    assert r.status_code == 200
    assert r.json() == {"items": [{"id": _SUPPLIER, "name": "Bravo Steel", "type": "CONTRACTOR"}]}
    sent = _qs(route.calls.last.request)
    assert sent["role"] == ["supplier"] and sent["q"] == ["bra"]
    assert sent["sort"] == ["name"]


@pytest.mark.anyio
async def test_contacts_lookup_requires_login() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/contacts/_lookup", params={"q": "x"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Global search page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_search_groups_counts_and_view_all(respx_mock: respx.MockRouter) -> None:
    hits = [
        {"id": _BILL, "kind": "bill", "title": "BILL-0042", "subtitle": "Bravo Steel — Posted — 450.00",
         "url": f"/bills/{_BILL}", "amount": "450.00", "date": "2026-11-01"},
        {"id": _SUPPLIER, "kind": "payment", "title": "EFT-9", "subtitle": None,
         "url": f"/payments/{_SUPPLIER}", "amount": "10.00", "date": "2026-11-02"},
    ]
    route = respx_mock.get(f"{_API}/api/v1/search").mock(
        return_value=Response(200, json={"query": "x", "hits": hits, "total": 2,
                                         "counts": {"bill": 37, "payment": 1}})
    )
    async with _client() as c:
        r = await c.get("/search", params={"q": "450"})
    assert r.status_code == 200
    assert _qs(route.calls.last.request)["limit"] == ["10"]
    body = r.text
    assert "38 results for" in body
    assert "Bills" in body and "Payments" in body
    assert "View all 37" in body and "kind=bill" in body
    assert f"/payments/{_SUPPLIER}" in body


@pytest.mark.anyio
@respx.mock
async def test_search_view_all_one_kind(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API}/api/v1/search").mock(
        return_value=Response(200, json={"query": "x", "hits": [], "total": 0, "counts": {"invoice": 120}})
    )
    async with _client() as c:
        r = await c.get("/search", params={"q": "acme", "kind": "invoice", "offset": "50"})
    sent = _qs(route.calls.last.request)
    assert sent["kind"] == ["invoice"] and sent["limit"] == ["50"] and sent["offset"] == ["50"]
    assert "Back to all results" in r.text
    assert "offset=100" in r.text  # Next page
    assert "offset=0" in r.text  # Previous page


@pytest.mark.anyio
@respx.mock
async def test_sort_links_keep_search_text_encoded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/invoices", params={"q": "A&B Pty"})
    assert "q=A%26B%20Pty" in r.text
