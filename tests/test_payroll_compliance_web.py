"""Batch 9 payroll compliance screens -- route smoke tests (engine mocked).

Single Touch Payroll page and actions, super owed and "mark paid", the
payroll year end and income statement, the wages bank file, payslip email,
pay items on the wage line, monthly pay runs, the leave tab on the employee.
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
_RUN = "11111111-1111-1111-1111-111111111111"
_EMP = "22222222-2222-2222-2222-222222222222"
_SUB = "44444444-4444-4444-4444-444444444444"
_SUP = "55555555-5555-5555-5555-555555555555"
_BANK = "66666666-6666-6666-6666-666666666666"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "TEST_SESSION_TOKEN"}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()}, follow_redirects=False,
    )


def _stp_item(state: str = "READY", overdue: bool = False) -> dict:
    return {
        "id": _SUB, "pay_run_id": _RUN, "event_type": "PAY", "status": "READY",
        "state": state, "due_date": "2026-11-30", "overdue": overdue,
        "report_period": {"start": "2026-11-23", "end": "2026-11-29",
                          "payment_date": "2026-11-30"},
        "totals": {"gross": "1520.00", "tax": "305.00"}, "payee_count": 1,
        "lodged_elsewhere_reference": None, "errors": [],
    }


# --------------------------------------------------------------------------- #
# Single Touch Payroll
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_stp_page_shows_overdue_and_lodging_off(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/stp").mock(return_value=Response(200, json={
        "items": [_stp_item(overdue=True)], "finalisations": [], "overdue_count": 1,
        "lodging_enabled": False,
        "lodging_off_message": "SAE Books cannot send this to the ATO yet.",
    }))
    async with _client() as c:
        r = await c.get("/payroll/stp")
    assert r.status_code == 200, r.text[:400]
    html = r.text
    assert "Single Touch Payroll" in html
    assert "SAE Books cannot send this to the ATO yet." in html
    assert "Overdue" in html and "1 pay event(s) are overdue." in html
    assert f'action="/payroll/stp/{_SUB}/lodged-elsewhere"' in html
    assert f'action="/payroll/stp/{_SUB}/check"' in html


@pytest.mark.anyio
@respx.mock
async def test_stp_check_problems_shown(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/api/v1/payroll/stp/{_SUB}/check").mock(
        return_value=Response(200, json={"ready": False, "problems": [
            "Your business details is missing: your ABN (Settings, Company)"]}))
    respx_mock.get(f"{_API}/api/v1/payroll/stp").mock(return_value=Response(200, json={
        "items": [_stp_item()], "finalisations": [], "overdue_count": 0,
        "lodging_enabled": False, "lodging_off_message": "off"}))
    async with _client() as c:
        r = await c.post(f"/payroll/stp/{_SUB}/check", data={"back": "/payroll/stp"})
        assert r.status_code == 303 and r.headers["location"] == "/payroll/stp"
        r = await c.get("/payroll/stp")
    assert "The ATO would reject this pay event" in r.text
    assert "your ABN (Settings, Company)" in r.text


@pytest.mark.anyio
@respx.mock
async def test_lodged_elsewhere_posts_reference(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/payroll/stp/{_SUB}/lodged-elsewhere").mock(
        return_value=Response(200, json=_stp_item("LODGED_ELSEWHERE")))
    async with _client() as c:
        r = await c.post(f"/payroll/stp/{_SUB}/lodged-elsewhere", data={
            "reference": "RCPT-9", "lodged_on": "2026-11-30",
            "back": f"/payroll/pay-runs/{_RUN}"})
    assert r.status_code == 303 and r.headers["location"] == f"/payroll/pay-runs/{_RUN}"
    assert _json.loads(route.calls.last.request.content) == {
        "reference": "RCPT-9", "lodged_on": "2026-11-30"}


@pytest.mark.anyio
async def test_back_cannot_leave_payroll() -> None:
    with respx.mock(assert_all_called=False) as rm:
        rm.post(f"{_API}/api/v1/payroll/stp/{_SUB}/lodged-elsewhere").mock(
            return_value=Response(200, json=_stp_item("LODGED_ELSEWHERE")))
        async with _client() as c:
            r = await c.post(f"/payroll/stp/{_SUB}/lodged-elsewhere", data={
                "reference": "R", "back": "https://evil.example/x"})
    assert r.headers["location"] == "/payroll/stp"


@pytest.mark.anyio
async def test_lodge_needs_declaration() -> None:
    with respx.mock(assert_all_called=False) as rm:
        route = rm.post(f"{_API}/api/v1/payroll/stp/{_SUB}/lodge").mock(
            return_value=Response(200, json={"status": "ACCEPTED"}))
        async with _client() as c:
            r = await c.post(f"/payroll/stp/{_SUB}/lodge", data={"signatory": "R"})
    assert r.status_code == 303 and not route.called


# --------------------------------------------------------------------------- #
# Super
# --------------------------------------------------------------------------- #


def _super_data() -> dict:
    return {
        "runs": [{
            "id": _SUP, "pay_run_id": _RUN, "period_start": "2026-11-23",
            "period_end": "2026-11-29", "payment_date": "2026-11-30", "status": "DRAFT",
            "total_amount": "182.40", "employee_count": 1, "due_date": "2026-12-29",
            "paid_on": None, "state": "DUE",
            "lines": [{"employee_name": "Jo Roofer", "fund_name": "Default Super",
                       "amount": "182.40", "first_contribution": True,
                       "member_number": None}],
        }],
        "funds": [{"super_fund_id": "f1", "fund_name": "Default Super",
                   "fund_usi": "STA0100AU01", "owed": "182.40",
                   "next_due": "2026-12-29", "overdue": False}],
        "total_owed": "182.40", "overdue_count": 0,
    }


@pytest.mark.anyio
@respx.mock
async def test_super_page_by_fund_and_mark_paid_form(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/super").mock(
        return_value=Response(200, json=_super_data()))
    respx_mock.get(f"{_API}/api/v1/bank_accounts").mock(return_value=Response(200, json={
        "items": [{"id": _BANK, "name": "Business Cheque", "code": "1-1110"}], "total": 1}))
    async with _client() as c:
        r = await c.get("/payroll/super")
    assert r.status_code == 200, r.text[:400]
    html = r.text
    assert "Default Super" in html and "2026-12-29" in html and "182.40" in html
    assert "first contribution, 20 business days" in html
    assert f'action="/payroll/super/{_SUP}/mark-paid"' in html
    assert f'href="/payroll/super/{_SUP}/file"' in html
    assert "Business Cheque" in html


@pytest.mark.anyio
@respx.mock
async def test_mark_paid_posts(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/payroll/super/{_SUP}/mark-paid").mock(
        return_value=Response(200, json={"amount": "182.40", "paid_on": "2026-12-01"}))
    async with _client() as c:
        r = await c.post(f"/payroll/super/{_SUP}/mark-paid", data={
            "bank_account_id": _BANK, "paid_on": "2026-12-01", "reference": ""})
    assert r.status_code == 303 and r.headers["location"] == "/payroll/super"
    assert _json.loads(route.calls.last.request.content) == {
        "bank_account_id": _BANK, "paid_on": "2026-12-01"}


@pytest.mark.anyio
@respx.mock
async def test_super_file_download(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/super_lodgements/{_SUP}/lines.csv").mock(
        return_value=Response(200, content=b"Pay Period Start\r\n"))
    async with _client() as c:
        r = await c.get(f"/payroll/super/{_SUP}/file")
    assert r.status_code == 200 and r.content.startswith(b"Pay Period Start")
    assert "attachment" in r.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# Year end
# --------------------------------------------------------------------------- #


def _year(blockers: list[str] | None = None) -> dict:
    return {
        "financial_year": 2027, "period_start": "2026-07-01", "period_end": "2027-06-30",
        "due_date": "2027-07-14", "overdue": False, "pay_run_count": 1,
        "employees": [{"employee_id": _EMP, "employee_number": "EMP-1", "name": "Jo Roofer",
                       "gross": "1520.00", "tax": "305.00", "super": "182.40",
                       "allowances": "0.00", "deductions": "0.00",
                       "bonuses_and_back_pay": "0.00", "net": "1215.00", "pay_runs": 1,
                       "last_payment_date": "2026-11-30", "finalised": False,
                       "problems": ["No tax treatment code."]}],
        "totals": {"gross": "1520.00", "tax": "305.00", "super": "182.40", "net": "1215.00"},
        "blockers": blockers or [], "can_finalise": not blockers,
        "finalisations": [], "lodging_enabled": False,
    }


@pytest.mark.anyio
@respx.mock
async def test_year_end_page_and_finalise(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/year-end/2027").mock(
        return_value=Response(200, json=_year()))
    route = respx_mock.post(f"{_API}/api/v1/payroll/year-end/2027/finalise").mock(
        return_value=Response(201, json={"id": _SUB}))
    async with _client() as c:
        r = await c.get("/payroll/year-end?year=2027")
        assert r.status_code == 200, r.text[:400]
        assert "Jo Roofer" in r.text and "1,520.00" in r.text
        assert "No tax treatment code." in r.text
        assert 'action="/payroll/year-end/2027/finalise"' in r.text
        assert f"/payroll/year-end/2027/income-statements/{_EMP}" in r.text
        r = await c.post("/payroll/year-end/2027/finalise", data={})
        assert not route.called
        r = await c.post("/payroll/year-end/2027/finalise", data={"confirm": "yes"})
    assert r.status_code == 303 and route.called


@pytest.mark.anyio
@respx.mock
async def test_year_end_blocked(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/year-end/2027").mock(
        return_value=Response(200, json=_year(["1 pay event(s) not yet lodged"])))
    async with _client() as c:
        r = await c.get("/payroll/year-end?year=2027")
    assert "cannot be finalised yet" in r.text and "not yet lodged" in r.text
    assert 'action="/payroll/year-end/2027/finalise"' not in r.text


@pytest.mark.anyio
@respx.mock
async def test_income_statement_printable(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/year-end/2027/income-statements/{_EMP}").mock(
        return_value=Response(200, json={
            "financial_year": 2027, "period_start": "2026-07-01", "period_end": "2027-06-30",
            "employer": {"name": "B09 Pty Ltd", "abn": "51824753556"},
            "employee": {"name": "Jo Roofer", "employee_number": "EMP-1",
                         "address": "2 Worker Rd, Cairns"},
            "amounts": {"gross": "1520.00", "tax": "305.00", "super": "182.40",
                        "allowances": "0.00", "deductions": "0.00",
                        "bonuses_and_back_pay": "0.00", "net": "1215.00"},
            "finalised": False, "status_note": "Not finalised yet."}))
    async with _client() as c:
        r = await c.get(f"/payroll/year-end/2027/income-statements/{_EMP}")
    assert r.status_code == 200
    assert "Income statement" in r.text and "51824753556" in r.text
    assert "Not finalised yet." in r.text and 'class="dark"' in r.text


# --------------------------------------------------------------------------- #
# Bank file, payslip email, settings
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_bank_file_download(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/pay-runs/{_RUN}/bank-file").mock(
        return_value=Response(200, content=b"0 header\r\n", headers={
            "content-disposition": 'attachment; filename="wages-2026-11-30.aba"'}))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}/bank-file")
    assert r.status_code == 200 and r.content == b"0 header\r\n"
    assert "wages-2026-11-30.aba" in r.headers["content-disposition"]


@pytest.mark.anyio
@respx.mock
async def test_bank_file_error_goes_back_with_message(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/payroll/pay-runs/{_RUN}/bank-file").mock(
        return_value=Response(422, json={"detail": "No bank details on record for: Jo"}))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}/bank-file")
    assert r.status_code == 303 and r.headers["location"] == f"/payroll/pay-runs/{_RUN}"


@pytest.mark.anyio
@respx.mock
async def test_email_payslips(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/payroll/pay-runs/{_RUN}/payslips/email").mock(
        return_value=Response(200, json={"results": [
            {"employee_id": _EMP, "name": "Jo", "to": "jo@example.com", "outcome": "sent"}],
            "sent": 1, "not_sent": 0}))
    async with _client() as c:
        r = await c.post(f"/payroll/pay-runs/{_RUN}/payslips/email", data={})
    assert r.status_code == 303 and route.called


@pytest.mark.anyio
@respx.mock
async def test_leave_provision_setting(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.put(f"{_API}/api/v1/payroll/settings").mock(
        return_value=Response(200, json={"leave_provision": True}))
    async with _client() as c:
        r = await c.post("/payroll/settings", data={"leave_provision": "yes"})
    assert r.status_code == 303
    assert _json.loads(route.calls.last.request.content) == {"leave_provision": True}


# --------------------------------------------------------------------------- #
# Pay items and monthly pay
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_line_sends_pay_items(respx_mock: respx.MockRouter) -> None:
    sent: list[dict] = []

    def _cap(request, *_):
        sent.append(_json.loads(request.content))
        return Response(201, json={})

    respx_mock.post(f"{_API}/api/v1/pay-runs/{_RUN}/wage-lines").mock(side_effect=_cap)
    async with _client() as c:
        r = await c.post(f"/payroll/pay-runs/{_RUN}/lines", data={
            "employee_id": _EMP, "ordinary_hours": "30", "overtime_hours": "0",
            "overtime_multiplier": "1.5",
            "leave_type_1": "ANNUAL", "leave_hours_1": "8", "leave_loading_1": "17.5",
            "allowance_kind_1": "TD", "allowance_description_1": "Tools",
            "allowance_amount_1": "20", "allowance_taxable_1": "yes",
            "allowance_kind_2": "MD", "allowance_amount_2": "$30",
            "deduction_kind_1": "F", "deduction_amount_1": "15",
            "extra_kind_1": "BONUS", "extra_amount_1": "2,600", "extra_super_1": "yes",
        })
    assert r.status_code == 303
    body = sent[0]
    assert body["leave_taken"] == [{"leave_type": "ANNUAL", "hours": "8",
                                    "loading_percent": "17.5"}]
    assert body["allowances"][0] == {"kind": "TD", "description": "Tools", "amount": "20",
                                     "taxable": True, "counts_for_super": False}
    assert body["allowances"][1]["taxable"] is False and body["allowances"][1]["amount"] == "30"
    assert body["deductions"] == [{"kind": "F", "description": "", "amount": "15"}]
    assert body["additional_payments"][0]["amount"] == "2600"
    assert body["additional_payments"][0]["counts_for_super"] is True


@pytest.mark.anyio
async def test_line_refuses_bad_item_amount() -> None:
    with respx.mock(assert_all_called=False) as rm:
        route = rm.post(f"{_API}/api/v1/pay-runs/{_RUN}/wage-lines").mock(
            return_value=Response(201, json={}))
        async with _client() as c:
            r = await c.post(f"/payroll/pay-runs/{_RUN}/lines", data={
                "employee_id": _EMP, "ordinary_hours": "38",
                "allowance_amount_1": "twenty"})
    assert r.status_code == 303 and not route.called


@pytest.mark.anyio
@respx.mock
async def test_monthly_run_fills_the_month(respx_mock: respx.MockRouter) -> None:
    sent: list[dict] = []

    def _cap(request, *_):
        sent.append(_json.loads(request.content))
        return Response(201, json={"id": _RUN})

    respx_mock.post(f"{_API}/api/v1/pay-runs").mock(side_effect=_cap)
    async with _client() as c:
        r = await c.post("/payroll/pay-runs/new", data={
            "frequency": "MONTHLY", "period_start": "2027-01-31", "period_end": "",
            "payment_date": "2027-02-28"})
    assert r.status_code == 303
    assert sent[0]["period_start"] == "2027-01-31" and sent[0]["period_end"] == "2027-02-28"


def test_month_end_matches_engine() -> None:
    from datetime import date

    from saebooks_web.routes.wages import month_end_for

    assert month_end_for(date(2026, 11, 1)) == date(2026, 11, 30)
    assert month_end_for(date(2027, 1, 15)) == date(2027, 2, 14)
    assert month_end_for(date(2027, 1, 31)) == date(2027, 2, 28)
    assert month_end_for(date(2026, 12, 1)) == date(2026, 12, 31)


# --------------------------------------------------------------------------- #
# Employee leave tab
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@respx.mock
async def test_employee_leave_opening_balance(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/leave/balances/{_EMP}/opening").mock(
        return_value=Response(200, json={}))
    async with _client() as c:
        r = await c.post(f"/employees/{_EMP}/leave", data={
            "action": "opening", "leave_type": "ANNUAL", "hours": "40", "as_at": "2026-07-01"})
    assert r.status_code == 303
    assert _json.loads(route.calls.last.request.content) == {
        "leave_type": "ANNUAL", "hours": "40", "as_at": "2026-07-01"}


@pytest.mark.anyio
async def test_employee_leave_adjust_needs_reason() -> None:
    with respx.mock(assert_all_called=False) as rm:
        route = rm.post(f"{_API}/api/v1/leave/balances/{_EMP}/adjust").mock(
            return_value=Response(200, json={}))
        async with _client() as c:
            r = await c.post(f"/employees/{_EMP}/leave", data={
                "action": "adjust", "leave_type": "ANNUAL", "hours": "-4"})
    assert r.status_code == 303 and not route.called


# --------------------------------------------------------------------------- #
# Pay run review page: finalised panels and pay items
# --------------------------------------------------------------------------- #


def _finalised_view() -> dict:
    line = {
        "line_id": "33333333-3333-3333-3333-333333333333", "employee_id": _EMP,
        "employee_number": "EMP-1", "employee_name": "Jo Roofer", "employment_basis": "F",
        "pay_frequency": "WEEKLY", "working_holiday_maker": False, "tfn_status": "PROVIDED",
        "ordinary_hours": "30.00", "overtime_hours": "0.00", "ordinary_rate": "40.00",
        "overtime_rate": "60.00", "overtime_multiplier": "1.5", "ordinary_pay": "1200.00",
        "overtime_pay": "0.00", "gross": "1596.00", "payg": "329.00", "super_amount": "189.12",
        "net": "1282.00", "ytd_gross": "1596.00", "ytd_tax": "329.00", "ytd_super": "189.12",
        "payg_scale_used": "SCALE_2", "payg_explanation": "Scale 2", "super_explanation": "SG",
        "computed": True, "super_fund_name": "Default Super",
        "bank": {"bsb": "062-000", "account_number": "12345678", "account_name": "Jo",
                 "source": "CONTACT"},
        "blockers": [], "warnings": [], "full_period_salary": False,
        "allowances": [{"type": "MD", "description": "Overtime meal", "amount": "30",
                        "is_taxable": False}],
        "deductions": [{"type": "F", "description": "Union fees", "amount": "15"}],
        "leave_taken": [{"leave_type": "ANNUAL", "hours": "8", "amount": "376.00",
                         "loading_percent": "17.5"}],
        "additional_payments": [], "nontaxable_allowances": "30.00",
        "deductions_total": "15.00", "leave_balances": {"ANNUAL": "14.92"},
    }
    return {
        "pay_run": {"id": _RUN, "company_id": "c" * 8 + "-cccc-cccc-cccc-" + "c" * 12,
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "period_start": "2026-11-23", "period_end": "2026-11-29",
                    "payment_date": "2026-11-30", "description": "Wages",
                    "status": "finalized", "journal_id": None, "version": 2,
                    "created_at": "2026-11-11T00:00:00Z", "updated_at": "2026-11-11T00:00:00Z",
                    "lines": [], "blockers": [], "warnings": []},
        "lines": [line], "total_gross": "1596.00", "total_payg": "329.00",
        "total_super": "189.12", "total_net": "1282.00", "accounts": [],
        "finalise_blockers": [], "can_finalise": False,
        "stp": {"id": _SUB, "state": "READY", "due_date": "2026-11-30", "overdue": True,
                "lodging_enabled": False, "errors": []},
        "super_due": {"id": _SUP, "state": "DUE", "total_amount": "189.12",
                      "due_date": "2026-12-29", "paid_on": None},
    }


@pytest.mark.anyio
@respx.mock
async def test_finalised_review_page_panels(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/pay-runs/{_RUN}/wages").mock(
        return_value=Response(200, json=_finalised_view()))
    respx_mock.get(f"{_API}/api/v1/employees").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    respx_mock.get(f"{_API}/api/v1/bank_accounts").mock(return_value=Response(200, json={
        "items": [{"id": _BANK, "name": "Cheque", "code": "1-1110", "apca_user_id": "123456"}]}))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}")
    assert r.status_code == 200, r.text[:400]
    html = r.text
    # STP panel: not lodged, overdue, lodging switched off said plainly.
    assert 'data-panel="stp"' in html and "Not lodged yet" in html and "Overdue" in html
    assert "not yet registered with the ATO" in html
    assert f'action="/payroll/stp/{_SUB}/lodged-elsewhere"' in html
    assert f'action="/payroll/stp/{_SUB}/lodge"' not in html
    # Super panel with its due date.
    assert 'data-panel="super"' in html and "2026-12-29" in html
    # Pay items on the line.
    assert "Overtime meal" in html and "no tax withheld" in html
    assert "Union fees" in html and "376.00" in html and "17.5% loading" in html
    assert "Leave owed now" in html and "14.92" in html
    # Bank file and payslip email.
    assert "Download bank file" in html
    assert f'action="/payroll/pay-runs/{_RUN}/payslips/email"' in html


@pytest.mark.anyio
@respx.mock
async def test_draft_review_page_has_more_pay_items(respx_mock: respx.MockRouter) -> None:
    view = _finalised_view()
    view["pay_run"]["status"] = "draft"
    view["stp"] = None
    view["super_due"] = None
    respx_mock.get(f"{_API}/api/v1/pay-runs/{_RUN}/wages").mock(
        return_value=Response(200, json=view))
    respx_mock.get(f"{_API}/api/v1/employees").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}")
    html = r.text
    assert 'id="more-pay-items"' in html
    for name in ("leave_hours_1", "allowance_amount_3", "deduction_amount_2",
                 "extra_amount_1", "full_period_salary"):
        assert f'name="{name}"' in html, name
    assert "Overtime meal" in html  # allowance kinds offered
    assert "Not built yet" in html  # termination payments said plainly


@pytest.mark.anyio
@respx.mock
async def test_employee_detail_leave_tab(respx_mock: respx.MockRouter) -> None:
    emp = {"id": _EMP, "employee_number": "EMP-1", "contact_id": None,
           "tfn_status": "APPLIED_FOR", "tfn_declaration_date": "2026-11-01",
           "super_choice_source": None, "employment_basis": "F", "version": 1,
           "start_date": "2026-07-01", "pay_frequency": "WEEKLY", "pay_basis": "HOURLY",
           "base_rate": "40.00", "weekly_hours": "38", "payslip_delivery": "EMAIL"}
    respx_mock.get(f"{_API}/api/v1/employees/{_EMP}").mock(
        return_value=Response(200, json=emp))
    respx_mock.get(f"{_API}/api/v1/leave/balances/{_EMP}").mock(return_value=Response(200, json=[
        {"id": "b1", "employee_id": _EMP, "leave_type": "ANNUAL", "balance_hours": "14.92",
         "opening_balance_hours": "20.00", "opening_balance_as_at": None, "version": 3}]))
    respx_mock.get(f"{_API}/api/v1/leave/history/{_EMP}").mock(return_value=Response(200, json={
        "items": [{"leave_type": "ANNUAL", "kind": "TAKE", "hours": "-8.00",
                   "balance_after": "12.00", "reason": "Taken in pay run",
                   "created_at": "2026-11-30T00:00:00"}]}))
    async with _client() as c:
        r = await c.get(f"/employees/{_EMP}")
    assert r.status_code == 200, r.text[:400]
    html = r.text
    assert 'id="leave"' in html and "14.92" in html and "Taken in pay run" in html
    assert f'action="/employees/{_EMP}/leave"' in html
    assert "28 days" in html and "2026-11-01" in html
    assert "Not recorded" in html  # super fund choice not recorded
