"""Batch 12 screens — jobs and stock (2026-11-13 missing-basics sweep).

Engine calls are mocked with respx; each test checks the screen renders
or the form posts the right body to the right engine endpoint.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.form_helpers import parse_lines
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_ITEM = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ACCT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_PROJ = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_QUOTE = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_PO = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_ADJ = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_INV = "11111111-1111-1111-1111-111111111111"
_CUST = "22222222-2222-2222-2222-222222222222"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps({"api_token": "t"}).encode())).decode()


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()},
    )


def _companies(m: respx.MockRouter) -> None:
    m.get(f"{_API}/api/v1/companies").mock(return_value=Response(200, json={"items": []}))


_ITEM_ROW = {
    "id": _ITEM, "sku": "BRK-1", "name": "Bracket", "description": None,
    "item_type": "inventory", "default_sale_price": "25", "purchase_price": "10",
    "income_account_id": _ACCT, "purchase_account_id": None,
    "inventory_account_id": _ACCT, "cogs_account_id": _ACCT,
    "sale_tax_code_id": None, "purchase_tax_code_id": None,
    "unit_of_measure": "each", "on_hand_qty": "3", "wac_cost": "10",
    "inventory_value": "30.00", "below_reorder_level": True, "reorder_level": "5",
    "cost_method": "WAC", "version": 2, "created_at": "2026-11-13T00:00:00Z",
    "archived_at": None, "company_id": _ACCT, "tenant_id": _ACCT,
}


def test_parse_lines_keeps_billable_and_order_line() -> None:
    lines = parse_lines({
        "lines[0][description]": "Steel", "lines[0][account_id]": _ACCT,
        "lines[0][billable]": "true", "lines[0][purchase_order_line_id]": _PO,
        "lines[0][item_id]": _ITEM,
    })
    assert lines[0]["billable"] == "true"
    assert lines[0]["purchase_order_line_id"] == _PO
    assert lines[0]["item_id"] == _ITEM


@pytest.mark.anyio
@respx.mock
async def test_item_options_feed_for_line_picker(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    respx_mock.get(f"{_API}/api/v1/items").mock(
        return_value=Response(200, json={"items": [_ITEM_ROW], "total": 1})
    )
    async with _client() as c:
        r = await c.get("/items/_options")
    assert r.status_code == 200
    row = r.json()["items"][0]
    assert row["sku"] == "BRK-1" and row["purchase_price"] == "10"
    assert "wac_cost" not in row  # only what the picker needs


@pytest.mark.anyio
@respx.mock
async def test_invoice_line_row_has_item_picker(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    for path in ("accounts", "tax_codes", "projects"):
        respx_mock.get(f"{_API}/api/v1/{path}").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/invoices/_add_line", params={"index": 3})
    assert r.status_code == 200
    assert 'name="lines[3][item_id]"' in r.text
    assert 'data-side="sale"' in r.text
    assert "/static/js/item_picker.js" in r.text


@pytest.mark.anyio
@respx.mock
async def test_items_list_filters_and_value(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    route = respx_mock.get(f"{_API}/api/v1/items").mock(
        return_value=Response(200, json={"items": [_ITEM_ROW], "total": 1, "total_value": "30.00",
                                         "limit": 200, "offset": 0})
    )
    async with _client() as c:
        r = await c.get("/items", params={"stock": "low_stock", "archived": "all", "q": "BRK"})
    assert r.status_code == 200
    sent = route.calls.last.request.url.params
    assert sent["stock"] == "low_stock" and sent["archived"] == "all" and sent["search"] == "BRK"
    assert "Low" in r.text and "30.00" in r.text


@pytest.mark.anyio
@respx.mock
async def test_item_restore_posts_if_match(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    route = respx_mock.post(f"{_API}/api/v1/items/{_ITEM}/restore").mock(
        return_value=Response(200, json=_ITEM_ROW)
    )
    async with _client() as c:
        r = await c.post(f"/items/{_ITEM}/restore", data={"version": "4"})
    assert r.status_code == 303
    assert route.calls.last.request.headers["If-Match"] == "4"


@pytest.mark.anyio
@respx.mock
async def test_stock_valuation_page_and_csv(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    report = {
        "as_of": "2026-11-13", "total_quantity": "3", "total_value": "30.00",
        "ledger_value": "25.00", "difference": "5.00", "note": None,
        "accounts": [{"account_id": _ACCT, "code": "1-1330", "name": "Stock", "balance": "25.00"}],
        "rows": [{"item_id": _ITEM, "sku": "BRK-1", "name": "Bracket", "unit_of_measure": "each",
                  "quantity": "3", "average_cost": "10", "value": "30.00",
                  "below_reorder_level": True, "archived": False}],
    }
    respx_mock.get(f"{_API}/api/v1/stock/valuation").mock(return_value=Response(200, json=report))
    async with _client() as c:
        page = await c.get("/stock")
        csv = await c.get("/stock", params={"format": "csv"})
    assert page.status_code == 200
    assert "disagree" in page.text  # the difference is explained in plain words
    assert csv.headers["content-type"].startswith("text/csv")
    assert "BRK-1" in csv.text


@pytest.mark.anyio
@respx.mock
async def test_stock_adjustment_create_and_post(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    create = respx_mock.post(f"{_API}/api/v1/stock/adjustments").mock(
        return_value=Response(201, json={"id": _ADJ})
    )
    post = respx_mock.post(f"{_API}/api/v1/stock/adjustments/{_ADJ}/post").mock(
        return_value=Response(200, json={"id": _ADJ})
    )
    async with _client() as c:
        r = await c.post("/stock/adjustments/new", data={
            "adjustment_date": "2026-11-13", "account_id": _ACCT, "reason": "stocktake",
            "lines[0][item_id]": _ITEM, "lines[0][counted_qty]": "8",
            "lines[1][item_id]": "", "lines[1][counted_qty]": "",
        })
        assert r.status_code == 303 and r.headers["location"] == f"/stock/adjustments/{_ADJ}"
        body = _json.loads(create.calls.last.request.content)
        assert body["lines"] == [{"item_id": _ITEM, "counted_qty": "8"}]
        r2 = await c.post(f"/stock/adjustments/{_ADJ}/post", data={"version": "1"})
    assert r2.status_code == 303
    assert post.calls.last.request.headers["If-Match"] == "1"


@pytest.mark.anyio
@respx.mock
async def test_project_page_shows_job_summary(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    project = {"id": _PROJ, "code": "J1", "name": "Shed", "status": "ACTIVE", "version": 1,
               "customer_id": _CUST, "company_id": _ACCT, "created_at": "2026-11-13T00:00:00Z",
               "archived_at": None}
    summary = {
        "income": "2000.00", "costs": "800.00", "profit": "1200.00", "margin_percent": "60.00",
        "unbilled_total": "1520.00", "unbilled_hours": "8", "unbilled_costs_value": "800.00",
        "hours": "8", "billable_hours": "8", "customer_id": _CUST, "customer_name": "Acme",
        "budget_income": {"budget": "5000.00", "actual": "2000.00", "remaining": "3000.00", "used_percent": "40.00"},
        "budget_cost": {"budget": None, "actual": "800.00", "remaining": None, "used_percent": None},
        "documents": {"invoices": [{"id": _INV, "number": "INV-1", "date": "2026-11-13", "status": "POSTED",
                                    "total": "2200.00", "contact_name": "Acme", "project_amount": "2000.00"}],
                      "credit_notes": [], "bills": [], "expenses": [], "purchase_orders": []},
    }
    unbilled = {"time": [{"id": _ITEM, "work_date": "2026-11-13", "hours": "8", "rate": "90",
                          "amount": "720.00", "description": "Welding", "missing_rate": False}],
                "costs": [], "time_total": "720.00", "costs_total": "0.00"}
    respx_mock.get(f"{_API}/api/v1/projects/{_PROJ}").mock(return_value=Response(200, json=project))
    respx_mock.get(f"{_API}/api/v1/projects/{_PROJ}/summary").mock(return_value=Response(200, json=summary))
    respx_mock.get(f"{_API}/api/v1/projects/{_PROJ}/unbilled").mock(return_value=Response(200, json=unbilled))
    respx_mock.get(f"{_API}/api/v1/tax_codes").mock(return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get(f"/projects/{_PROJ}")
        r2 = await c.get(f"/projects/{_PROJ}", params={"tab": "unbilled"})
    assert r.status_code == 200
    assert ("1,200.00" in r.text or "1200.00" in r.text) and "INV-1" in r.text and "Acme" in r.text
    assert 'name="time_entry_ids"' in r2.text and "Create draft invoice" in r2.text


@pytest.mark.anyio
@respx.mock
async def test_bill_unbilled_redirects_to_invoice(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    route = respx_mock.post(f"{_API}/api/v1/projects/{_PROJ}/bill-unbilled").mock(
        return_value=Response(201, json={"invoice_id": _INV})
    )
    async with _client() as c:
        r = await c.post(f"/projects/{_PROJ}/bill-unbilled",
                         data={"time_entry_ids": [_ITEM], "markup_percent": "10"})
    assert r.status_code == 303 and r.headers["location"] == f"/invoices/{_INV}"
    body = _json.loads(route.calls.last.request.content)
    assert body["time_entry_ids"] == [_ITEM] and body["markup_percent"] == "10"


@pytest.mark.anyio
@respx.mock
async def test_quote_progress_claim_posts_mode(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    route = respx_mock.post(f"{_API}/api/v1/quotes/{_QUOTE}/convert-to-invoice").mock(
        return_value=Response(200, json={"invoice_id": _INV, "quote": {}})
    )
    async with _client() as c:
        r = await c.post(f"/quotes/{_QUOTE}/convert-to-invoice",
                         data={"version": "3", "mode": "progress", "by": "amount", "amount": "10000"})
    assert r.status_code == 303 and r.headers["location"] == f"/invoices/{_INV}"
    assert _json.loads(route.calls.last.request.content) == {"mode": "progress", "amount": "10000"}


@pytest.mark.anyio
@respx.mock
async def test_quote_revise_goes_to_new_draft(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    respx_mock.post(f"{_API}/api/v1/quotes/{_QUOTE}/revise").mock(
        return_value=Response(201, json={"id": _INV, "revision": 2})
    )
    async with _client() as c:
        r = await c.post(f"/quotes/{_QUOTE}/revise", data={"version": "2"})
    assert r.status_code == 303 and r.headers["location"] == f"/quotes/{_INV}/edit"


@pytest.mark.anyio
@respx.mock
async def test_order_email_sends_and_returns(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    route = respx_mock.post(f"{_API}/api/v1/purchase_orders/{_PO}/send-email").mock(
        return_value=Response(200, json={"mode": "sent", "number": "PO-000001", "log_id": "x"})
    )
    async with _client() as c:
        r = await c.post(f"/purchase_orders/{_PO}/email", data={
            "from_addr": "a@example.com", "to": "s@example.com, t@example.com",
            "subject": "Order", "body_html": "<p>Hi</p>",
        })
    assert r.status_code == 303 and r.headers["location"] == f"/purchase_orders/{_PO}"
    body = _json.loads(route.calls.last.request.content)
    assert body["to"] == ["s@example.com", "t@example.com"]


@pytest.mark.anyio
@respx.mock
async def test_sales_by_item_report(respx_mock: respx.MockRouter) -> None:
    _companies(respx_mock)
    respx_mock.get(f"{_API}/api/v1/stock/reports/sales-by-item").mock(return_value=Response(200, json={
        "rows": [{"item_id": _ITEM, "sku": "BRK-1", "name": "Bracket", "quantity": "2",
                  "sales": "60.00", "cost_of_sales": "24.00", "margin": "36.00", "margin_percent": "60.00"}],
        "totals": {"quantity": "2", "sales": "60.00", "cost_of_sales": "24.00", "margin": "36.00"},
    }))
    async with _client() as c:
        r = await c.get("/stock/reports/sales-by-item", params={"date_from": "2026-07-01", "date_to": "2026-11-13"})
    assert r.status_code == 200
    assert "Bracket" in r.text and "36.00" in r.text and "60.00%" in r.text
