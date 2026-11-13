"""Batch 14 web — owner money and assets (P0-44, P1-47, P1-50, P1-73, P1-74).

Engine calls are mocked with respx; each test checks the payload the screen
sends and what it renders.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from urllib.parse import urlencode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_BANK = "aaaa0000-0000-0000-0000-000000000001"
_CARD = "aaaa0000-0000-0000-0000-000000000002"
_OWNER = "aaaa0000-0000-0000-0000-000000000003"
_DRAW = "aaaa0000-0000-0000-0000-000000000004"
_PHONE = "aaaa0000-0000-0000-0000-000000000005"
_ASSET_ID = "fa000000-0000-0000-0000-000000000001"
_LOAN_ID = "1a000000-0000-0000-0000-000000000001"
_RUN_ID = "7a000000-0000-0000-0000-000000000001"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode())).decode()


_SESSION = _cookie({"api_token": "test-token-b14"})


def _client(**kw) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION},
        follow_redirects=False,
        **kw,
    )


def _accounts_by_type(request) -> Response:
    t = request.url.params.get("account_type")
    rows = {
        "ASSET": [{"id": _BANK, "code": "1-1110", "name": "Bank", "account_type": "ASSET"}],
        "LIABILITY": [
            {"id": _CARD, "code": "2-1110", "name": "Credit Card", "account_type": "LIABILITY"},
            {"id": _OWNER, "code": "2-2400", "name": "Owner loan", "account_type": "LIABILITY"},
        ],
        "EQUITY": [{"id": _DRAW, "code": "3-1200", "name": "Drawings", "account_type": "EQUITY"}],
        "EXPENSE": [{"id": _PHONE, "code": "6-2800", "name": "Telephone", "account_type": "EXPENSE"}],
    }.get(t, [])
    return Response(200, json={"items": rows, "total": len(rows)})


def _form(data: list[tuple[str, str]]) -> dict:
    return {"content": urlencode(data), "headers": {"Content-Type": "application/x-www-form-urlencoded"}}


# --------------------------------------------------------------------------- #
# P1-74 TPAR                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_tpar_list_renders_and_counts_contractors(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/tpar").mock(return_value=Response(200, json={"items": [{
        "id": _RUN_ID, "fy_start": "2025-07-01", "fy_end": "2026-06-30", "status": "DRAFT",
        "total_payee_count": 2, "total_gross_amount": "12000.00", "total_gst_amount": "1090.91",
    }], "total": 1}))
    respx_mock.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": [
        {"id": "c1", "name": "Sparky", "is_tpar_supplier": True},
        {"id": "c2", "name": "Shop", "is_tpar_supplier": False},
    ]}))
    async with _client() as c:
        r = await c.get("/tpar")
    assert r.status_code == 200
    assert "Taxable payments annual report" in r.text
    assert "1 contact is marked as a reportable contractor." in r.text
    assert f"/tpar/{_RUN_ID}" in r.text
    assert "coming soon" not in r.text.split("Taxable payments annual report")[1][:200]


@pytest.mark.anyio
@respx.mock
async def test_tpar_generate_redirects_to_run(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/tpar").mock(
        return_value=Response(201, json={"id": _RUN_ID, "status": "DRAFT"})
    )
    async with _client() as c:
        r = await c.post("/tpar", data={"fy_start": "2025-07-01"})
    assert r.status_code == 303 and r.headers["location"] == f"/tpar/{_RUN_ID}"
    assert _json.loads(route.calls.last.request.content) == {"fy_start": "2025-07-01"}


@pytest.mark.anyio
@respx.mock
async def test_tpar_detail_flags_missing_abn(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/tpar/{_RUN_ID}").mock(return_value=Response(200, json={
        "id": _RUN_ID, "fy_start": "2025-07-01", "fy_end": "2026-06-30", "status": "DRAFT",
        "total_payee_count": 1, "total_gross_amount": "5500.00", "total_gst_amount": "500.00",
    }))
    respx_mock.get(f"{_API}/api/v1/tpar/{_RUN_ID}/lines").mock(return_value=Response(200, json={
        "items": [{"contact_id": "c1", "payee_name": "Sparky", "payee_abn": None,
                   "gross_paid": "5500.00", "gst_paid": "500.00", "bill_count": 2,
                   "expense_count": 0}], "total": 1}))
    async with _client() as c:
        r = await c.get(f"/tpar/{_RUN_ID}")
    assert r.status_code == 200
    assert "1 contractor has no ABN" in r.text
    assert f"/tpar/{_RUN_ID}/download.bde" in r.text
    assert f"/tpar/{_RUN_ID}/finalise" in r.text


@pytest.mark.anyio
@respx.mock
async def test_contact_create_sends_tpar_flag(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/contacts").mock(
        return_value=Response(201, json={"id": "c9", "name": "Sparky"})
    )
    async with _client() as c:
        r = await c.post("/contacts/new", data={
            "name": "Sparky", "contact_type": "SUPPLIER", "is_tpar_supplier": "on",
        })
    assert r.status_code == 303, r.text
    assert _json.loads(route.calls.last.request.content)["is_tpar_supplier"] is True


# --------------------------------------------------------------------------- #
# P1-50 transfers                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_transfer_form_offers_purposes_and_prefills_card(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/accounts").mock(side_effect=_accounts_by_type)
    async with _client() as c:
        r = await c.get(f"/transfers/new?purpose=card_payment&to_account_id={_CARD}")
    assert r.status_code == 200
    assert "What is this?" in r.text
    assert 'value="card_payment"' in r.text and "checked" in r.text.split('value="card_payment"')[1][:400]
    assert "data-submit-once" in r.text
    assert f'value="{_CARD}" data-type="LIABILITY" selected' in r.text


@pytest.mark.anyio
@respx.mock
async def test_transfer_post_sends_purpose_and_key(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/transfers").mock(
        return_value=Response(201, json={"id": "t1"})
    )
    async with _client() as c:
        r = await c.post("/transfers/new", data={
            "idempotency_key": "k-123", "purpose": "card_payment",
            "from_account_id": _BANK, "to_account_id": _CARD,
            "amount": "100.00", "transfer_date": "2026-11-01",
        })
    assert r.status_code == 303 and r.headers["location"] == "/transfers/t1"
    sent = route.calls.last.request
    assert sent.headers["X-Idempotency-Key"] == "k-123"
    assert _json.loads(sent.content)["purpose"] == "card_payment"


@pytest.mark.anyio
@respx.mock
async def test_transfer_reused_key_explains(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/transfers").mock(return_value=Response(
        422, json={"code": "idempotency_key_conflict", "message": "reused"}))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(side_effect=_accounts_by_type)
    async with _client() as c:
        r = await c.post("/transfers/new", data={
            "idempotency_key": "k-1", "from_account_id": _BANK, "to_account_id": _CARD,
            "amount": "101.00", "transfer_date": "2026-11-01",
        })
    assert r.status_code == 422
    assert "already recorded from this form" in r.text
    assert 'value="k-1"' not in r.text, "a fresh key is issued"


# --------------------------------------------------------------------------- #
# P1-47 owner money                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_expense_paid_personally_with_private_share(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/expenses").mock(
        return_value=Response(201, json={"id": "e1", "reference": None})
    )
    async with _client() as c:
        r = await c.post("/expenses/new", **_form([
            ("idempotency_key", "k-e"), ("expense_date", "2026-11-01"),
            ("paid_by", "owner"), ("owner_account_id", ""), ("payment_account_id", _BANK),
            ("lines[0][description]", "Phone"), ("lines[0][account_id]", _PHONE),
            ("lines[0][unit_price]", "100"), ("lines[0][private_pct]", "30"),
            ("lines[1][description]", "Parking"), ("lines[1][account_id]", _PHONE),
            ("lines[1][unit_price]", "10"), ("lines[1][private_pct]", "0"),
        ]))
    assert r.status_code == 303, r.text
    body = _json.loads(route.calls.last.request.content)
    assert body["paid_by"] == "owner"
    assert "payment_account_id" not in body, "blank owner account = the owner loan account"
    assert body["lines"][0]["private_pct"] == "30"
    assert "private_pct" not in body["lines"][1]


@pytest.mark.anyio
@respx.mock
async def test_owner_money_page_and_reimburse(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/owner_money/reimbursements").mock(return_value=Response(200, json={
        "items": [{"id": "e1", "number": "EXP-1", "reference": None, "expense_date": "2026-10-01",
                   "notes": None, "total": "55.00", "owner_account_id": _OWNER,
                   "owner_account_code": "2-2400", "owner_account_name": "Owner loan"}],
        "total": "55.00", "count": 1}))
    respx_mock.get(f"{_API}/api/v1/owner_money/settings").mock(return_value=Response(200, json={
        "owner_loan_account": {"id": _OWNER, "code": "2-2400", "name": "Owner loan"},
        "drawings_account": None, "missing_standard_accounts": []}))
    respx_mock.get(f"{_API}/api/v1/owner_money/owner_loan_report").mock(return_value=Response(200, json={
        "account": {"id": _OWNER, "code": "2-2400", "name": "Owner loan"},
        "closing_balance": "55.00", "business_owes_owner": "55.00", "owner_owes_business": "0",
        "unreimbursed_expenses": "55.00", "lines": [], "date_to": "2026-11-13",
        "date_from": None, "opening_balance": "0"}))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(side_effect=_accounts_by_type)
    async with _client() as c:
        r = await c.get("/owner-money")
    assert r.status_code == 200
    assert "Reimburse selected" in r.text and "EXP-1" in r.text

    route = respx_mock.post(f"{_API}/api/v1/owner_money/reimburse").mock(return_value=Response(
        201, json={"transfer_id": "t9", "amount": "55.00", "expense_ids": ["e1"]}))
    async with _client() as c:
        r = await c.post("/owner-money/reimburse", **_form([
            ("idempotency_key", "k-r"), ("expense_ids", "e1"), ("expense_ids", "e2"),
            ("from_account_id", _BANK), ("reimbursement_date", "2026-11-02"),
        ]))
    assert r.status_code == 303 and r.headers["location"] == "/transfers/t9"
    body = _json.loads(route.calls.last.request.content)
    assert body["expense_ids"] == ["e1", "e2"]
    assert route.calls.last.request.headers["X-Idempotency-Key"] == "k-r"


# --------------------------------------------------------------------------- #
# P1-73 fixed asset posting                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_asset_create_sends_paid_purchase(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/fixed_assets").mock(
        return_value=Response(201, json={"id": _ASSET_ID})
    )
    async with _client() as c:
        r = await c.post("/fixed-assets/new", data={
            "idempotency_key": "k-a", "name": "Laptop", "depreciation_model_id": "asset_5_year_linear",
            "cost_account_id": _BANK, "accum_dep_account_id": _BANK, "dep_expense_account_id": _PHONE,
            "purchase_date": "2026-07-01", "cost": "2000",
            "purchase_method": "paid", "paid_from_account_id": _BANK,
            "purchase_gst_amount": "200", "purchase_tax_code_id": "tc1",
        })
    assert r.status_code == 303
    body = _json.loads(route.calls.last.request.content)
    assert body["purchase"] == {"method": "paid", "paid_from_account_id": _BANK,
                                "gst_amount": "200", "tax_code_id": "tc1"}


@pytest.mark.anyio
@respx.mock
async def test_asset_create_on_finance_points_to_loans(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/fixed_assets")
    respx_mock.get(url__regex=rf"{_API}/api/v1/.*").mock(
        return_value=Response(200, json={"items": []})
    )
    async with _client() as c:
        r = await c.post("/fixed-assets/new", data={
            "name": "Ute", "depreciation_model_id": "m", "cost_account_id": _BANK,
            "accum_dep_account_id": _BANK, "dep_expense_account_id": _PHONE,
            "purchase_date": "2026-07-01", "cost": "50000", "purchase_method": "finance",
        })
    assert r.status_code == 422
    assert "recorded together with the loan" in r.text
    assert not route.called


@pytest.mark.anyio
@respx.mock
async def test_asset_dispose_sends_proceeds_account(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/fixed_assets/{_ASSET_ID}/dispose").mock(
        return_value=Response(200, json={"id": _ASSET_ID, "status": "disposed"})
    )
    async with _client() as c:
        r = await c.post(f"/fixed-assets/{_ASSET_ID}/dispose", data={
            "version": "2", "disposal_date": "2026-11-01", "proceeds": "1500",
            "proceeds_account_id": _BANK,
        })
    assert r.status_code == 303
    assert _json.loads(route.calls.last.request.content)["proceeds_account_id"] == _BANK


@pytest.mark.anyio
@respx.mock
async def test_asset_list_shows_register_mismatch(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/fixed_assets").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API}/api/v1/fixed_assets/tie_out").mock(return_value=Response(200, json={
        "in_step": False, "as_at": "2026-11-13",
        "accounts": [{"cost_account_code": "1-3410", "cost_account_name": "Vehicles",
                      "cost_difference": "-2000.00", "accum_dep_difference": "0.00"}],
        "unlinked_assets": [{"id": _ASSET_ID, "code": "AST-1", "name": "Laptop", "cost": "2000"}],
    }))
    async with _client() as c:
        r = await c.get("/fixed-assets")
    assert r.status_code == 200
    assert "do not agree" in r.text
    assert "1 asset is not linked to a purchase" in r.text


# --------------------------------------------------------------------------- #
# P0-44 loans                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_loan_create_on_asset_sends_asset(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/loans").mock(
        return_value=Response(201, json={"id": _LOAN_ID, "fixed_asset_id": _ASSET_ID})
    )
    async with _client() as c:
        r = await c.post("/loans/new", data={
            "idempotency_key": "k-l", "name": "Ute finance", "kind": "chattel_mortgage",
            "funding": "asset", "asset_name": "Hilux", "asset_cost": "50000",
            "asset_gst_amount": "5000", "asset_tax_code_id": "tc1", "deposit": "5000",
            "deposit_account_id": _BANK, "depreciation_model_id": "asset_5_year_linear",
            "cost_account_id": _BANK, "accum_dep_account_id": _BANK,
            "dep_expense_account_id": _PHONE, "annual_rate_pct": "7", "term_periods": "60",
            "frequency": "monthly", "start_date": "2026-07-15",
            "first_repayment_date": "2026-08-15", "balloon": "15000",
            "liability_account_id": _OWNER, "interest_account_id": _PHONE,
        })
    assert r.status_code == 303 and r.headers["location"] == f"/loans/{_LOAN_ID}"
    body = _json.loads(route.calls.last.request.content)
    assert body["funding"] == "asset" and "principal" not in body
    assert body["asset"]["cost"] == "50000" and body["asset"]["deposit_account_id"] == _BANK
    assert body["balloon"] == "15000"
    assert route.calls.last.request.headers["X-Idempotency-Key"] == "k-l"


@pytest.mark.anyio
@respx.mock
async def test_loan_detail_and_repayment(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/loans/{_LOAN_ID}").mock(return_value=Response(200, json={
        "id": _LOAN_ID, "name": "Equipment loan", "kind": "loan", "status": "active",
        "principal": "12000.00", "annual_rate_pct": "6.0000", "term_periods": 12,
        "frequency": "monthly", "balloon": "0.00", "repayment_amount": "1032.80",
        "interest_method": "as_you_go", "version": 1, "lender_contact_id": None,
        "opening_journal_id": "je1", "fixed_asset_id": None,
        "position": {"repayments_made": 0, "principal_outstanding": "12000.00",
                     "unexpired_interest": "0.00", "interest_charged": "0.00"},
        "next_repayment": {"number": 1, "due_date": "2026-08-01", "payment": "1032.80",
                           "interest": "60.00"},
        "schedule": [{"number": 1, "due_date": "2026-08-01", "payment": "1032.80",
                      "interest": "60.00", "principal": "972.80", "balance_after": "11027.20"}],
        "repayments": [],
    }))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(side_effect=_accounts_by_type)
    async with _client() as c:
        r = await c.get(f"/loans/{_LOAN_ID}")
    assert r.status_code == 200
    assert "Record a repayment" in r.text and "60.00" in r.text
    assert "Cancel loan entered by mistake" in r.text

    route = respx_mock.post(f"{_API}/api/v1/loans/{_LOAN_ID}/repayments").mock(
        return_value=Response(201, json={"amount": "1032.80", "principal": "972.80", "interest": "60.00"})
    )
    async with _client() as c:
        r = await c.post(f"/loans/{_LOAN_ID}/repayments", data={
            "idempotency_key": "k-p", "amount": "1032.80", "repayment_date": "2026-08-01",
            "bank_account_id": _BANK, "interest": "",
        })
    assert r.status_code == 303
    body = _json.loads(route.calls.last.request.content)
    assert body == {"amount": "1032.80", "repayment_date": "2026-08-01", "bank_account_id": _BANK}


@pytest.mark.anyio
@respx.mock
async def test_loan_repay_from_bank_line(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bank_statement_lines/bsl1").mock(return_value=Response(
        200, json={"id": "bsl1", "txn_date": "2026-08-01", "description": "LOAN", "amount": "-1032.80"}))
    respx_mock.get(f"{_API}/api/v1/loans").mock(return_value=Response(200, json={"items": [
        {"id": _LOAN_ID, "name": "Equipment loan", "repayment_amount": "1032.80", "frequency": "monthly"}]}))
    async with _client() as c:
        r = await c.get("/loans/repay?statement_line_id=bsl1")
    assert r.status_code == 200 and "Record and match" in r.text
    route = respx_mock.post(f"{_API}/api/v1/loans/{_LOAN_ID}/repayments").mock(
        return_value=Response(201, json={"amount": "1032.80"}))
    async with _client() as c:
        r = await c.post("/loans/repay", data={
            "loan_id": _LOAN_ID, "bank_statement_line_id": "bsl1", "idempotency_key": "k-b"})
    assert r.status_code == 303 and r.headers["location"] == f"/loans/{_LOAN_ID}"
    assert _json.loads(route.calls.last.request.content) == {"bank_statement_line_id": "bsl1"}


@pytest.mark.anyio
@respx.mock
async def test_loan_preview_partial(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/loans/preview").mock(return_value=Response(200, json={
        "principal": "12000", "repayment_amount": "1032.80", "total_interest": "393.60",
        "total_repayments": "12393.60", "schedule": []}))
    async with _client() as c:
        r = await c.get("/loans/preview?principal=12000&annual_rate_pct=6&term_periods=12"
                        "&frequency=monthly&first_repayment_date=2026-08-01&funding=cash")
    assert r.status_code == 200 and "Each repayment" in r.text
    assert _json.loads(route.calls.last.request.content)["principal"] == "12000"


@pytest.mark.anyio
@respx.mock
async def test_statement_line_offers_loan_repayment(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bank_statement_lines").mock(return_value=Response(200, json={
        "items": [
            {"id": "bslout", "txn_date": "2026-08-01", "description": "LOAN DEBIT",
             "amount": "-1032.80", "balance": "0.00", "reference": None,
             "status": "UNMATCHED", "account_id": _BANK, "version": 1},
            {"id": "bslin", "txn_date": "2026-08-02", "description": "DEPOSIT",
             "amount": "500.00", "balance": "500.00", "reference": None,
             "status": "UNMATCHED", "account_id": _BANK, "version": 1},
        ],
        "total": 2, "limit": 50, "offset": 0}))
    respx_mock.get(url__regex=rf"{_API}/api/v1/(accounts|bank_accounts).*").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get("/bank-statement-lines")
    assert r.status_code == 200, r.text[:500]
    assert "/loans/repay?statement_line_id=bslout" in r.text
    assert "/loans/repay?statement_line_id=bslin" not in r.text
