"""Batch 4 (2026-11-13) — the reconciliation loop screens.

Create / split / find-and-match-many / ignore / rule suggestion / unmatch
with void / statement panel and live difference / report, finish, reopen /
bank feeds / real import preview / bank account Books tab.
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

_ACCT = "aaaaaaaa-1111-2222-3333-444444444444"
_OTHER = "abababab-1111-2222-3333-444444444444"
_BSL = "bbbbbbbb-1111-2222-3333-444444444444"
_JE = "cccccccc-1111-2222-3333-444444444444"
_REC = "dddddddd-1111-2222-3333-444444444444"
_GL = "eeeeeeee-1111-2222-3333-444444444444"
_CONTACT = "ffffffff-1111-2222-3333-444444444444"
_BILL = "12121212-1111-2222-3333-444444444444"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode("utf-8"))).decode("utf-8")


_SESSION = _cookie({"api_token": "test-token-b04", "user_role": "bookkeeper"})
_API = settings.api_url.rstrip("/")

_LINE = {
    "id": _BSL, "account_id": _ACCT, "txn_date": "2026-10-15",
    "description": "TELSTRA BILL", "amount": "-110.00", "reference": "TB1",
    "status": "UNMATCHED", "version": 1, "contact_id": None,
}
_DETAIL = {**_LINE, "remaining": "-110.00", "allocated": "0", "matches": [],
           "suggested_rule": None, "reconciliation_id": None, "ignored_reason": None}
_ACCOUNTS = [
    {"id": _ACCT, "code": "1-1110", "name": "Business Cheque", "balance_kind": "balance",
     "ledger_balance": "100.00", "balance": "90.00", "lines_to_match": 1,
     "last_reconciled_date": None, "open_reconciliation_id": None},
    {"id": _OTHER, "code": "1-1120", "name": "Savings", "balance_kind": "balance"},
]
_SUMMARY = {
    "statement_date": "2026-10-31", "statement_balance": "-110.00", "book_balance": "-110.00",
    "outstanding_total": "0", "outstanding_in_total": "0", "outstanding_out_total": "0",
    "booked_later_total": "0", "ignored_total": "0", "difference": "0.00",
    "to_match_count": 0, "to_match_total": "0", "cleared_count": 1, "ignored_count": 0,
    "statement_check": None, "can_finish": True,
    "outstanding": [], "booked_later": [], "ignored": [], "to_match": [],
}
_PRINTED = {k: _SUMMARY[k] for k in (
    "statement_balance", "book_balance", "outstanding_total", "outstanding_in_total",
    "outstanding_out_total", "booked_later_total", "ignored_total", "difference",
    "to_match_total")}
_REPORT = {
    "reconciliation": {"id": _REC, "account_id": _ACCT, "statement_date": "2026-10-31",
                       "statement_balance": "-110.00", "status": "OPEN",
                       "finished_by": None, "finished_at": None, "reopened_at": None,
                       "reopen_reason": None},
    "account": {"id": _ACCT, "code": "1-1110", "name": "Business Cheque",
                "account_type": "ASSET", "balance_kind": "balance"},
    "summary": _SUMMARY,
    "printed": _PRINTED,
}


def _client(follow: bool = False) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION}, follow_redirects=follow,
    )


def _quiet(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/companies").mock(return_value=Response(200, json={"items": []}))
    respx_mock.get(f"{_API}/api/v1/modules").mock(return_value=Response(200, json={"modules": []}))


def _options(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [{"id": _CONTACT, "name": "Telstra"}]}))
    respx_mock.get(f"{_API}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [{"id": _GL, "code": "6-1000", "name": "Phone"}]}))
    respx_mock.get(f"{_API}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": [{"id": "t1", "code": "GST", "name": "GST"}]}))


# ---------------------------------------------------------------------------
# Reconcile screen with a statement in progress
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_lines_page_shows_statement_panel_and_rule_suggestion(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/reconciliation/accounts").mock(return_value=Response(200, json=_ACCOUNTS))
    line = {**_DETAIL, "suggested_rule": {"id": "r1", "name": "Phone bills", "account_id": _GL,
                                          "tax_code": "GST", "contact_id": None}}
    respx_mock.get(f"{_API}/api/v1/reconciliation/lines").mock(return_value=Response(200, json={
        "items": [line], "total": 1, "counts": {"TO_MATCH": 1, "MATCHED": 4, "IGNORED": 0}}))
    respx_mock.get(f"{_API}/api/v1/reconciliation/statements").mock(return_value=Response(200, json={
        "items": [{"id": _REC, "status": "OPEN", "statement_date": "2026-10-31"}]}))
    respx_mock.get(f"{_API}/api/v1/reconciliation/statements/{_REC}").mock(
        return_value=Response(200, json=_REPORT))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_ACCT}/lines")
    assert r.status_code == 200
    assert "Closing balance on the statement" in r.text
    assert "Difference" in r.text and "Finish" in r.text
    assert "Bank rule: Phone bills" in r.text and "Use this rule" in r.text
    assert "Matched <span class=\"ink-3\">(4)</span>" in r.text


@pytest.mark.anyio
@respx.mock
async def test_statement_post_and_live_difference(respx_mock) -> None:
    _quiet(respx_mock)
    start = respx_mock.post(f"{_API}/api/v1/reconciliation/statements").mock(
        return_value=Response(201, json=_REPORT))
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_ACCT}/statement",
                         data={"statement_date": "2026-10-31", "statement_balance": "1,234.50"})
    assert r.status_code == 303
    assert _json.loads(start.calls.last.request.content) == {
        "account_id": _ACCT, "statement_date": "2026-10-31", "statement_balance": "1234.50"}

    respx_mock.get(f"{_API}/api/v1/reconciliation/preview").mock(
        return_value=Response(200, json={"summary": {**_SUMMARY, "difference": "5.00"},
                                         "printed": {**_PRINTED, "difference": "5.00"}}))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_ACCT}/difference",
                        params={"statement_date": "2026-10-31", "statement_balance": "-105"})
    assert r.status_code == 200
    assert "5.00" in r.text and "Difference" in r.text


# ---------------------------------------------------------------------------
# Create from a line
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_create_form_and_expense_payload(respx_mock) -> None:
    _quiet(respx_mock)
    _options(respx_mock)
    respx_mock.get(f"{_API}/api/v1/bank_statement_lines/{_BSL}").mock(return_value=Response(200, json=_LINE))
    respx_mock.get(f"{_API}/api/v1/reconciliation/line_detail/{_BSL}").mock(return_value=Response(200, json=_DETAIL))
    respx_mock.get(f"{_API}/api/v1/reconciliation/accounts").mock(return_value=Response(200, json=_ACCOUNTS))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_BSL}/create", params={"account_id": _ACCT})
    assert r.status_code == 200
    assert "Spending (no bill)" in r.text and "Paying a supplier bill" in r.text
    assert "Transfer to another account" in r.text
    assert "GST code" in r.text

    cm = respx_mock.post(f"{_API}/api/v1/reconciliation/create_and_match").mock(
        return_value=Response(201, json={"bsl": {**_LINE, "status": "MATCHED"}, "record_type": "expense",
                                         "record_id": "x", "journal_entry_id": _JE, "match_id": "m"}))
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_BSL}/create", data={
            "account_id": _ACCT, "kind": "expense", "contact_id": _CONTACT,
            "gl_account_id": _GL, "tax_code_id": "t1", "description": "Phone"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/reconciliation/{_ACCT}/lines"
    sent = _json.loads(cm.calls.last.request.content)
    assert sent["record_type"] == "expense"
    assert sent["expense"]["account_id"] == _GL and sent["expense"]["tax_code_id"] == "t1"
    assert sent["expense"]["contact_id"] == _CONTACT
    assert "amount" not in sent


@pytest.mark.anyio
@respx.mock
async def test_create_bill_payment_and_transfer_payloads(respx_mock) -> None:
    _quiet(respx_mock)
    cm = respx_mock.post(f"{_API}/api/v1/reconciliation/create_and_match").mock(
        return_value=Response(201, json={"bsl": {**_LINE, "status": "MATCHED"}, "record_type": "payment",
                                         "record_id": "x", "journal_entry_id": _JE, "match_id": "m",
                                         "counterpart_bsl_id": None}))
    async with _client() as c:
        await c.post(f"/reconciliation/{_BSL}/create", data={
            "account_id": _ACCT, "kind": "bill_payment", "contact_id": _CONTACT,
            f"alloc_{_BILL}": "110.00", "amount": "110"})
    sent = _json.loads(cm.calls.last.request.content)
    assert sent["record_type"] == "payment"
    assert sent["payment"]["allocations"] == [{"bill_id": _BILL, "amount": "110.00"}]
    assert sent["amount"] == "110"

    async with _client() as c:
        await c.post(f"/reconciliation/{_BSL}/create", data={
            "account_id": _ACCT, "kind": "transfer", "other_account_id": _OTHER})
    sent = _json.loads(cm.calls.last.request.content)
    assert sent["record_type"] == "transfer"
    assert sent["transfer"]["other_account_id"] == _OTHER


@pytest.mark.anyio
@respx.mock
async def test_create_engine_error_is_shown_plainly(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.post(f"{_API}/api/v1/reconciliation/create_and_match").mock(
        return_value=Response(422, json={"detail": "A receipt can only match a deposit"}))
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_BSL}/create", data={
            "account_id": _ACCT, "kind": "receipt", "gl_account_id": _GL})
    assert r.status_code == 303
    assert "/create?" in r.headers["location"]


# ---------------------------------------------------------------------------
# Split, find, match many
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_split_posts_parts(respx_mock) -> None:
    _quiet(respx_mock)
    sp = respx_mock.post(f"{_API}/api/v1/reconciliation/split_create").mock(
        return_value=Response(201, json={"bsl": {**_LINE, "status": "MATCHED"}, "parts": []}))
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_BSL}/split", data={
            "account_id": _ACCT, "direction": "out",
            "gl_account_id_0": _GL, "amount_0": "60", "tax_code_id_0": "t1",
            "gl_account_id_1": _GL, "amount_1": "50", "contact_id_1": _CONTACT})
    assert r.status_code == 303
    parts = _json.loads(sp.calls.last.request.content)["parts"]
    assert [p["amount"] for p in parts] == ["60", "50"]
    assert parts[0]["record_type"] == "expense" and parts[0]["expense"]["tax_code_id"] == "t1"
    assert parts[1]["expense"]["contact_id"] == _CONTACT


@pytest.mark.anyio
@respx.mock
async def test_find_page_and_match_many(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/reconciliation/candidates/{_BSL}").mock(return_value=Response(200, json={
        "line": _LINE,
        "items": [{"target_id": _JE, "entry_date": "2026-10-14", "source_label": "Payment PAY-0007",
                   "contact_name": "Telstra", "description": "Phone", "amount": "-110.00",
                   "matched": "0", "remaining": "-110.00", "confidence": "HIGH"}]}))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_BSL}/find", params={"account_id": _ACCT, "q": "tel"})
    assert r.status_code == 200
    assert "Payment PAY-0007" in r.text and "Telstra" in r.text and "very likely" in r.text

    mm = respx_mock.post(f"{_API}/api/v1/reconciliation/match_many").mock(
        return_value=Response(200, json={**_LINE, "status": "MATCHED"}))
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_BSL}/match-many",
                         data={"account_id": _ACCT, "pick": [_JE], f"amount_{_JE}": "110.00"})
    assert r.status_code == 303 and r.headers["location"] == f"/reconciliation/{_ACCT}/lines"
    assert _json.loads(mm.calls.last.request.content)["items"] == [
        {"target_type": "JOURNAL_ENTRY", "target_id": _JE, "amount": "110.00"}]


@pytest.mark.anyio
async def test_match_many_needs_a_pick() -> None:
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_BSL}/match-many", data={"account_id": _ACCT})
    assert r.status_code == 303 and "/find" in r.headers["location"]


# ---------------------------------------------------------------------------
# Ignore, put back, accept rule
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ignore_unignore_accept_rule(respx_mock) -> None:
    _quiet(respx_mock)
    ig = respx_mock.post(f"{_API}/api/v1/reconciliation/ignore/{_BSL}").mock(
        return_value=Response(200, json={**_LINE, "status": "IGNORED"}))
    un = respx_mock.post(f"{_API}/api/v1/reconciliation/unignore/{_BSL}").mock(
        return_value=Response(200, json=_LINE))
    ar = respx_mock.post(f"{_API}/api/v1/reconciliation/accept_rule/{_BSL}").mock(
        return_value=Response(200, json={**_LINE, "status": "MATCHED"}))
    async with _client() as c:
        r1 = await c.post(f"/reconciliation/{_BSL}/ignore", data={"account_id": _ACCT, "reason": "Duplicate import"})
        r2 = await c.post(f"/reconciliation/{_BSL}/unignore", data={"account_id": _ACCT})
        r3 = await c.post(f"/reconciliation/{_BSL}/accept-rule", data={"account_id": _ACCT})
    assert all(r.status_code == 303 for r in (r1, r2, r3))
    assert _json.loads(ig.calls.last.request.content) == {"reason": "Duplicate import"}
    assert un.called and ar.called
    assert r2.headers["location"].endswith("?tab=ignored")


# ---------------------------------------------------------------------------
# Unmatch: confirm page offers to void records created from the line
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_unmatch_confirm_and_void_created(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/reconciliation/unmatch_preview/{_BSL}").mock(return_value=Response(200, json={
        "line": _LINE, "created": [{"type": "expense", "id": "e1", "label": "Expense EXP-0012"}],
        "locked": False, "locked_by_statement_date": None}))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_BSL}/unmatch", params={"account_id": _ACCT})
    assert r.status_code == 200
    assert "Expense EXP-0012" in r.text and 'name="void_created"' in r.text

    um = respx_mock.post(f"{_API}/api/v1/reconciliation/unmatch/{_BSL}").mock(
        return_value=Response(200, json={**_LINE, "voided": [{"label": "Expense EXP-0012"}]}))
    async with _client() as c:
        r = await c.post(f"/reconciliation/{_BSL}/unmatch", data={"account_id": _ACCT, "void_created": "1"})
    assert r.status_code == 303
    assert _json.loads(um.calls.last.request.content) == {"void_created": True}


@pytest.mark.anyio
@respx.mock
async def test_unmatch_confirm_locked_line(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/reconciliation/unmatch_preview/{_BSL}").mock(return_value=Response(200, json={
        "line": _LINE, "created": [], "locked": True, "locked_by_statement_date": "2026-09-30"}))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_BSL}/unmatch", params={"account_id": _ACCT})
    assert "finished to 2026-09-30" in r.text
    assert 'action="/reconciliation/' + _BSL + '/unmatch"' not in r.text


# ---------------------------------------------------------------------------
# Report, finish, reopen, history
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_report_finish_reopen_history(respx_mock) -> None:
    _quiet(respx_mock)
    finished = {**_REPORT, "reconciliation": {**_REPORT["reconciliation"], "status": "FINISHED",
                                              "finished_by": "api:abc", "finished_at": "2026-11-01T10:00:00"}}
    respx_mock.get(f"{_API}/api/v1/reconciliation/statements/{_REC}").mock(return_value=Response(200, json=finished))
    async with _client() as c:
        r = await c.get(f"/reconciliation/statements/{_REC}")
    assert r.status_code == 200
    assert "Balance on the bank statement" in r.text
    assert "Print or save as PDF" in r.text and "Download spreadsheet" in r.text
    assert "Why reopen it?" in r.text

    fin = respx_mock.post(f"{_API}/api/v1/reconciliation/statements/{_REC}/finish").mock(
        return_value=Response(422, json={"detail": "The difference is 5.00. It must be nil before you can finish."}))
    rp = respx_mock.post(f"{_API}/api/v1/reconciliation/statements/{_REC}/reopen").mock(
        return_value=Response(200, json=_REPORT))
    async with _client() as c:
        r1 = await c.post(f"/reconciliation/statements/{_REC}/finish", data={})
        r2 = await c.post(f"/reconciliation/statements/{_REC}/reopen", data={"reason": "Wrong fee"})
    assert fin.called and r1.status_code == 303
    assert _json.loads(rp.calls.last.request.content) == {"reason": "Wrong fee"}

    respx_mock.get(f"{_API}/api/v1/reconciliation/accounts").mock(return_value=Response(200, json=_ACCOUNTS))
    respx_mock.get(f"{_API}/api/v1/reconciliation/statements").mock(return_value=Response(200, json={"items": [
        {"id": _REC, "statement_date": "2026-10-31", "statement_balance_printed": "110.00",
         "status": "FINISHED", "finished_by": "api:abc", "finished_at": "2026-11-01T10:00:00"}]}))
    async with _client() as c:
        r = await c.get(f"/reconciliation/{_ACCT}/history")
    assert r.status_code == 200 and "2026-10-31" in r.text and "Finished" in r.text


@pytest.mark.anyio
@respx.mock
async def test_report_csv_is_passed_through(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/reconciliation/statements/{_REC}/report.csv").mock(
        return_value=Response(200, text="Bank reconciliation,1-1110\n",
                              headers={"content-disposition": 'attachment; filename="x.csv"'}))
    async with _client() as c:
        r = await c.get(f"/reconciliation/statements/{_REC}/report.csv")
    assert r.status_code == 200 and r.text.startswith("Bank reconciliation")
    assert r.headers["content-type"].startswith("text/csv")


# ---------------------------------------------------------------------------
# Bank feeds
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_feeds_page_states(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/reconciliation/accounts").mock(return_value=Response(200, json=_ACCOUNTS))
    route = respx_mock.get(f"{_API}/api/v1/bank-feeds/connections")
    route.mock(return_value=Response(404, json={"detail": "Not Found"}))
    async with _client() as c:
        r = await c.get("/banking/feeds")
    assert r.status_code == 200 and "not part of your plan" in r.text

    route.mock(return_value=Response(200, json=[{"id": "c1", "siss_client_id": "stub-1",
                                                  "account_id": _ACCT, "status": "pending_consent",
                                                  "last_sync_cursor": None,
                                                  "created_at": "2026-11-01T00:00:00",
                                                  "updated_at": "2026-11-01T00:00:00"}]))
    async with _client() as c:
        r = await c.get("/banking/feeds")
    assert "test mode" in r.text and "Waiting for bank approval" in r.text and "Sync now" in r.text

    respx_mock.post(f"{_API}/api/v1/bank-feeds/sync").mock(return_value=Response(200, json={
        "connection_id": "c1", "stub": True, "inserted": 0, "next_cursor": None, "has_more": False}))
    async with _client() as c:
        r = await c.post("/banking/feeds/c1/sync", data={})
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Import: real preview, bookkeeper allowed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_import_real_preview_for_bookkeeper(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.post(f"{_API}/api/v1/imports/wizards").mock(return_value=Response(201, json={"wizard_id": "w1"}))
    step = respx_mock.post(url__regex=rf"^{_API}/api/v1/imports/wizards/[^/]+/step$").mock(
        return_value=Response(200, json={"step": 1}))
    respx_mock.post(f"{_API}/api/v1/imports/bank/preview").mock(return_value=Response(200, json={
        "format": "qif", "format_label": "QIF file", "count": 3, "new_count": 2, "duplicate_count": 1,
        "date_from": "2026-10-01", "date_to": "2026-10-31", "money_in": "100.00", "money_out": "45.50",
        "flip_signs": True, "warnings": ["Most lines in this file are money in."],
        "lines": [{"txn_date": "2026-10-01", "description": "COFFEE", "reference": None,
                   "amount": "-45.50", "already_imported": True}]}))
    async with _client() as c:
        r = await c.post("/admin/imports/bank/preview",
                         files={"file": ("s.qif", b"!Type:Bank\n", "text/plain")},
                         data={"account_id": _ACCT, "format": "qif", "flip_signs": "1"})
    assert r.status_code == 200
    assert "QIF file" in r.text and "already imported" in r.text
    assert "Import 2 new lines" in r.text
    assert "Most lines in this file are money in." in r.text
    patch = _json.loads(step.calls.last.request.content)["patch"]
    assert patch["format"] == "qif" and patch["flip_signs"] is True


# ---------------------------------------------------------------------------
# Bank account page: balances, actions, Books tab
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bank_account_books_tab(respx_mock) -> None:
    _quiet(respx_mock)
    respx_mock.get(f"{_API}/api/v1/bank_accounts/{_ACCT}").mock(return_value=Response(200, json={
        "id": _ACCT, "code": "1-1110", "name": "Business Cheque", "version": 1,
        "account_kind": "BANK_CHECKING", "bsb": "062-000", "bank_account_number": "1234",
        "created_at": "2026-01-01T00:00:00", "archived_at": None}))
    respx_mock.get(f"{_API}/api/v1/bank_statement_lines").mock(return_value=Response(200, json={"items": [], "total": 0}))
    respx_mock.get(f"{_API}/api/v1/bank_accounts/{_ACCT}/register").mock(return_value=Response(200, json={
        "account": {"id": _ACCT, "balance_kind": "balance"},
        "balances": {"book_balance": "400.00", "book_balance_printed": "400.00",
                     "statement_balance": "390.00", "statement_balance_printed": "390.00",
                     "statement_lines": 3, "lines_to_match": 1,
                     "last_reconciled_date": "2026-09-30", "last_reconciled_balance_printed": "250.00"},
        "opening_balance": "0", "total": 1, "limit": 100, "offset": 0,
        "items": [{"entry_id": _JE, "entry_ref": "JE-9", "entry_date": "2026-10-02",
                   "description": "To savings", "status": "POSTED", "amount": "-100.00",
                   "money_in": None, "money_out": "100.00", "running_balance": "400.00",
                   "cleared": True, "source_label": "Transfer TR-1", "source_type": "transfer",
                   "source_id": "t1", "contact_name": None}]}))
    async with _client() as c:
        r = await c.get(f"/bank-accounts/{_ACCT}", params={"view": "books"})
    assert r.status_code == 200
    assert "Balance in the books" in r.text and "400.00" in r.text
    assert "Transfer TR-1" in r.text and "Cleared" in r.text
    assert "Reconcile" in r.text and "Record a transfer" in r.text
