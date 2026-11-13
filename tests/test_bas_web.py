"""Batch 5 — GST and BAS screens.

* BAS summary shows GST-inclusive labels, W1/W2, nil result, drill links
* GST detail page lists the transactions behind a label + the tie-out
* BAS periods list, save, record lodged, settle preview and settle
* dated void: reversal date sent to the engine, lodged-period confirm page
* expense form: "Amounts include GST" switch on by default, sent to engine
* PAYG worksheet reads W1/W2 from the engine (no pay-run list call)
* dashboard GST tile + BAS due chip
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


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "test-token-abc"}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _client(follow: bool = False) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()},
        follow_redirects=follow,
    )


_BAS = {
    "from_date": "2026-07-01",
    "to_date": "2026-09-30",
    "basis": "accrual",
    "g1_total_sales": 11000.0,
    "g2_export_sales": 0.0,
    "g3_other_gst_free_sales": 0.0,
    "g10_capital_acquisitions": 0.0,
    "g11_other_acquisitions": 5500.0,
    "label_1a_gst_on_sales": 1000.0,
    "label_1b_gst_on_purchases": 500.0,
    "net_gst": 500.0,
    "remit_or_refund": "REMIT",
    "registration_effective_date": None,
    "g1_pre_registration": 0.0,
    "g1_post_registration": 11000.0,
    "w1_total_wages": 4000.0,
    "w2_amounts_withheld": 600.0,
    "label_4_payg_withheld": 600.0,
    "label_5a_payg_instalment": 0.0,
    "label_8a_owed_to_ato": 1600.0,
    "label_8b_owed_by_ato": 500.0,
    "label_9_amount_payable": 1100.0,
    "result": "PAYABLE",
    "pay_run_count": 2,
    "withholding_account_code": "2-1420",
    "withholding_account_credits": 600.0,
    "withholding_difference": 0.0,
    "notes": [],
}


@pytest.mark.anyio
@respx.mock
async def test_bas_summary_inclusive_labels_and_drill_links(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/bas_summary.*$").mock(
        return_value=Response(200, json=_BAS)
    )
    async with _client() as client:
        resp = await client.get("/reports/bas-summary?from_date=2026-07-01&to_date=2026-09-30&basis=cash")
    assert resp.status_code == 200
    t = resp.text
    assert "Total sales, including GST" in t
    assert "Other purchases, including GST" in t
    assert "always 0" not in t
    assert "/reports/gst-detail?label=G1&amp;from_date=2026-07-01" in t or "/reports/gst-detail?label=G1&from_date=2026-07-01" in t
    assert "W1" in t and "W2" in t
    assert "To pay the ATO" in t
    assert "1100.00" in t or "1,100.00" in t
    # The basis toggle is passed straight to the engine.
    assert route.calls.last.request.url.params["basis"] == "cash"


@pytest.mark.anyio
@respx.mock
async def test_bas_summary_nil(respx_mock: respx.MockRouter) -> None:
    nil = {**_BAS, "g1_total_sales": 0.0, "g11_other_acquisitions": 0.0,
           "label_1a_gst_on_sales": 0.0, "label_1b_gst_on_purchases": 0.0,
           "net_gst": 0.0, "remit_or_refund": "NIL", "result": "NIL",
           "label_9_amount_payable": 0.0, "w2_amounts_withheld": 0.0}
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/bas_summary.*$").mock(
        return_value=Response(200, json=nil)
    )
    async with _client() as client:
        resp = await client.get("/reports/bas-summary")
    assert "Nothing to pay" in resp.text
    assert "REFUND" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_gst_detail_page(respx_mock: respx.MockRouter) -> None:
    detail = {
        "label": "1A", "from_date": "2026-07-01", "to_date": "2026-09-30",
        "basis": "accrual", "total": "100.00",
        "rows": [{
            "date": "2026-07-10", "entry_date": "2026-07-10", "ref": "JE-000123",
            "journal_entry_id": "e1", "source_type": "invoice", "source_id": "inv-1",
            "description": "INV-0042", "accounts": ["2-1310 GST Collected"],
            "portion": "1", "amount": "100.00",
        }],
        "tie_out": {"account_code": "2-1310", "account_movement": "100.00",
                    "label_total": "100.00", "difference": "0.00",
                    "settlement_journals": [], "explained": True},
    }
    respx_mock.get(url__regex=rf"^{_API}/api/v1/bas/gst_detail.*$").mock(
        return_value=Response(200, json=detail)
    )
    async with _client() as client:
        resp = await client.get("/reports/gst-detail?label=1A&from_date=2026-07-01&to_date=2026-09-30")
    assert resp.status_code == 200
    assert "GST on sales" in resp.text
    assert 'href="/invoices/inv-1"' in resp.text
    assert "Movement on account 2-1310" in resp.text


_PERIODS = {
    "basis": "accrual", "frequency": "quarterly",
    "items": [{
        "period_id": "p1", "from_date": "2026-07-01", "to_date": "2026-09-30",
        "label": "July to September 2026", "status": "open", "due_date": "2026-10-28",
        "return": {"id": "r1", "status": "ready", "figures": {"G1": "11000.00", "1A": "1000.00", "1B": "500.00", "9": "1100.00"},
                   "generated_at": "2026-10-01T00:00:00Z", "lodged_on": None, "lodgement_reference": None},
        "settlements": [],
    }],
    "waiting": [{"from_date": "2026-04-01", "to_date": "2026-06-30", "label": "April to June 2026", "due_date": "2026-07-28"}],
}


@pytest.mark.anyio
@respx.mock
async def test_bas_periods_list(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bas/periods").mock(return_value=Response(200, json=_PERIODS))
    async with _client() as client:
        resp = await client.get("/bas")
    assert resp.status_code == 200
    t = resp.text
    assert "July to September 2026" in t
    assert "Record as lodged" in t
    assert "/bas/returns/r1/lodge" in t
    assert "Settle with the ATO" in t
    assert "April to June 2026" in t


@pytest.mark.anyio
@respx.mock
async def test_bas_save_and_lodge_post_to_engine(respx_mock: respx.MockRouter) -> None:
    save = respx_mock.post(f"{_API}/api/v1/bas/returns").mock(
        return_value=Response(201, json={"id": "r1"})
    )
    lodge = respx_mock.post(f"{_API}/api/v1/bas/returns/r1/lodge").mock(
        return_value=Response(200, json={"id": "r1", "status": "lodged",
                                         "lock": {"requested": True, "locked_through": "2026-09-30", "note": None}})
    )
    async with _client() as client:
        r1 = await client.post("/bas/returns", data={"from_date": "2026-07-01", "to_date": "2026-09-30", "payg_instalment": "250"})
        r2 = await client.post("/bas/returns/r1/lodge", data={"lodged_on": "2026-10-20", "reference": "ATO-1", "lock_period": "1"})
    assert r1.status_code == 303 and r2.status_code == 303
    assert _json.loads(save.calls.last.request.content) == {
        "from_date": "2026-07-01", "to_date": "2026-09-30", "payg_instalment": "250"}
    assert _json.loads(lodge.calls.last.request.content) == {
        "lodged_on": "2026-10-20", "reference": "ATO-1", "lock_period": True}


@pytest.mark.anyio
@respx.mock
async def test_settle_preview_and_post(respx_mock: respx.MockRouter) -> None:
    plan = {
        "from_date": "2026-07-01", "to_date": "2026-09-30",
        "lines": [{"account_id": "a1", "account_code": "2-1310", "account_name": "GST Collected",
                   "description": "Clear GST on sales (1A)", "debit": "1000.00", "credit": "0"}],
        "ato_account": {"id": "ato", "code": "2-1340", "name": "ATO"},
        "amount_payable": "500.00", "result": "PAYABLE", "problems": [],
        "already_settled": [], "can_settle": True,
    }
    respx_mock.get(url__regex=rf"^{_API}/api/v1/bas/settle/preview.*$").mock(return_value=Response(200, json=plan))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/bank_accounts.*$").mock(
        return_value=Response(200, json={"items": [{"id": "bank1", "code": "1-1110", "name": "Business account"}], "total": 1})
    )
    settle = respx_mock.post(f"{_API}/api/v1/bas/settle").mock(
        return_value=Response(201, json={"settlement_ref": "JE-1", "payment_ref": "JE-2"})
    )
    async with _client() as client:
        page = await client.get("/bas/settle?from_date=2026-07-01&to_date=2026-09-30")
        assert page.status_code == 200
        assert "Clear GST on sales (1A)" in page.text
        assert "Business account" in page.text
        r = await client.post("/bas/settle", data={
            "from_date": "2026-07-01", "to_date": "2026-09-30", "settlement_date": "2026-10-20",
            "pay_now": "1", "pay_from_account_id": "bank1", "payment_date": "2026-10-21"})
    assert r.status_code == 303 and r.headers["location"] == "/bas"
    assert _json.loads(settle.calls.last.request.content)["pay_from_account_id"] == "bank1"


_INV_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.anyio
@respx.mock
async def test_void_sends_reversal_date(respx_mock: respx.MockRouter) -> None:
    void = respx_mock.post(f"{_API}/api/v1/invoices/{_INV_ID}/void").mock(return_value=Response(200, json={}))
    async with _client() as client:
        r = await client.post(f"/invoices/{_INV_ID}/void", data={"version": "3", "reversal_date": "2026-11-13"})
    assert r.status_code == 303
    assert _json.loads(void.calls.last.request.content) == {"reversal_date": "2026-11-13"}


@pytest.mark.anyio
@respx.mock
async def test_void_in_lodged_period_asks_first(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/bills/{_INV_ID}/void").mock(
        return_value=Response(409, json={
            "code": "lodged_period",
            "message": "Voiding on 10/08/2026 changes the BAS for 01/07/2026 to 30/09/2026, which is marked lodged.",
            "period_start": "2026-07-01", "period_end": "2026-09-30", "reversal_date": "2026-08-10",
        })
    )
    async with _client() as client:
        r = await client.post(f"/bills/{_INV_ID}/void", data={"version": "2"})
    assert r.status_code == 409
    assert "This changes a lodged BAS" in r.text
    assert 'name="confirm_lodged_period"' in r.text
    assert f'action="/bills/{_INV_ID}/void"' in r.text


@pytest.mark.anyio
@respx.mock
async def test_void_version_conflict_still_flashes(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/bills/{_INV_ID}/void").mock(
        return_value=Response(409, json={"detail": "version mismatch", "current": {}})
    )
    async with _client() as client:
        r = await client.post(f"/bills/{_INV_ID}/void", data={"version": "2"})
    assert r.status_code == 303


@pytest.mark.anyio
@respx.mock
async def test_expense_create_sends_inclusive_flag(respx_mock: respx.MockRouter) -> None:
    create = respx_mock.post(f"{_API}/api/v1/expenses").mock(
        return_value=Response(201, json={"id": "e1"})
    )
    async with _client() as client:
        await client.post("/expenses/new", data={
            "payment_account_id": "bank1", "expense_date": "2026-11-13",
            "amounts_include_tax": "1",
            "lines[0][description]": "Receipt", "lines[0][account_id]": "acc",
            "lines[0][unit_price]": "110", "lines[0][tax_override]": "10.00",
        })
    body = _json.loads(create.calls.last.request.content)
    assert body["amounts_include_tax"] is True
    assert body["lines"][0]["tax_override"] == "10.00"


@pytest.mark.anyio
@respx.mock
async def test_bas_payg_reads_engine_labels(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/bas_summary.*$").mock(
        return_value=Response(200, json=_BAS)
    )
    pay_runs = respx_mock.get(url__regex=rf"^{_API}/api/v1/pay-runs.*$").mock(
        return_value=Response(200, json={"items": []})
    )
    async with _client() as client:
        resp = await client.get("/reports/bas-payg?from_date=2026-07-01&to_date=2026-09-30")
    assert resp.status_code == 200
    assert "finalised pay runs only" in resp.text
    assert "4000.00" in resp.text or "4,000.00" in resp.text
    assert not pay_runs.called


def test_dashboard_bas_tile_renders() -> None:
    from fastapi.templating import Jinja2Templates

    from saebooks_web.routes.dashboard import _TEMPLATES as T  # noqa: N811

    assert isinstance(T, Jinja2Templates)
    tpl = T.env.get_template("_partials/dashboard_bas_tile.html")
    html = tpl.render(
        bas_position={
            "as_of": "2026-11-13",
            "current": {"from_date": "2026-10-01", "label": "October to December 2026",
                        "set_aside": "1600.00", "net_gst": "1000.00", "tax_withheld": "600.00"},
            "due": {"label": "July to September 2026", "due_date": "2026-10-28",
                    "days_to_due": -16, "overdue": True},
        },
        money=lambda v, decimals=2: f"${v:,.{decimals}f}",
        _=lambda s: s,
    )
    assert "GST to set aside" in html
    assert "$1,600" in html
    assert "was due 2026-10-28 and is not marked lodged" in html
