"""Wages pay-run screens (/payroll/pay-runs) -- route smoke tests.

The engine is mocked with respx; these prove each screen renders what the
engine returns, sends the engine the right request, and never posts PAYG,
super or net figures of its own.
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
_LINE = "33333333-3333-3333-3333-333333333333"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "TEST_SESSION_TOKEN"}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()}, follow_redirects=False,
    )


def _run(status: str = "draft") -> dict:
    return {
        "id": _RUN, "company_id": "c" * 8 + "-cccc-cccc-cccc-" + "c" * 12,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "period_start": "2026-11-23", "period_end": "2026-11-29",
        "payment_date": "2026-11-30", "description": "Wages", "status": status,
        "journal_id": None, "version": 1,
        "created_at": "2026-11-11T00:00:00Z", "updated_at": "2026-11-11T00:00:00Z",
        "lines": [{"id": _LINE, "pay_run_id": _RUN, "employee_id": _EMP,
                   "gross": "1330.00", "tax": "200.00", "super_amount": "159.60",
                   "net": "1130.00", "created_at": "2026-11-11T00:00:00Z"}],
        "blockers": [], "warnings": [],
    }


def _view(status: str = "draft", *, can: bool = True) -> dict:
    return {
        "pay_run": _run(status),
        "lines": [{
            "line_id": _LINE, "employee_id": _EMP, "employee_number": "EMP-0001",
            "employee_name": "Backpacker Roofer", "employment_basis": "C",
            "pay_frequency": "WEEKLY", "working_holiday_maker": True,
            "tfn_status": "PROVIDED", "ordinary_hours": "38.00", "overtime_hours": "0.00",
            "ordinary_rate": "35.00", "overtime_rate": "52.50", "overtime_multiplier": "1.5",
            "ordinary_pay": "1330.00", "overtime_pay": "0.00",
            "gross": "1330.00", "payg": "200.00", "super_amount": "159.60", "net": "1130.00",
            "ytd_gross": "1330.00", "ytd_tax": "200.00", "ytd_super": "159.60",
            "payg_scale_used": "WHM_SCH15_15",
            "payg_explanation": "Working holiday maker, registered employer (Schedule 15)",
            "super_explanation": "SG 12.00% x ordinary time earnings $1330.00 = $159.60",
            "computed": True, "super_fund_name": "Test Super",
            "bank": {"bsb": "062-000", "account_number": "12345678",
                     "account_name": "B Roofer", "source": "EMPLOYEE"},
            "blockers": [] if can else ["visa expired"],
            "warnings": ["Working holiday maker: 5 months"],
        }],
        "total_gross": "1330.00", "total_payg": "200.00",
        "total_super": "159.60", "total_net": "1130.00",
        "accounts": [{"purpose": "Wages expense", "code": "6-2430", "status": "OK",
                      "account_name": "Wages & Salaries", "problem": None},
                     {"purpose": "PAYG WH", "code": "2-1420", "status": "MISSING",
                      "account_name": None, "problem": "No active account 2-1420"}],
        "finalise_blockers": [] if can else ["EMP-0001: visa expired"],
        "can_finalise": can,
    }


@pytest.mark.anyio
@respx.mock
async def test_list_renders_totals_and_form(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/pay-runs").mock(
        return_value=Response(200, json={"items": [_run("finalized")], "total": 1,
                                         "limit": 100, "offset": 0}))
    respx_mock.get(f"{_API}/api/v1/payroll/settings").mock(
        return_value=Response(200, json={"leave_provision": False,
                                         "stp_lodging_enabled": False}))
    respx_mock.get(f"{_API}/api/v1/payroll/stp").mock(return_value=Response(200, json={
        "items": [{"id": "s1", "pay_run_id": _RUN, "state": "READY", "overdue": True}],
        "finalisations": [], "overdue_count": 1, "lodging_enabled": False}))
    async with _client() as c:
        r = await c.get("/payroll/pay-runs")
    assert r.status_code == 200, r.text[:400]
    assert "Wages pay runs" in r.text
    assert 'action="/payroll/pay-runs/new"' in r.text
    assert "1,130.00" in r.text and "Finalised" in r.text
    # Batch 9: monthly pay, the STP column with its overdue flag, leave setting.
    assert 'value="MONTHLY"' in r.text
    assert "Not reported" in r.text and "Overdue" in r.text
    assert 'action="/payroll/settings"' in r.text


@pytest.mark.anyio
@respx.mock
async def test_create_fills_week_end_and_redirects(respx_mock: respx.MockRouter) -> None:
    sent: list[dict] = []

    def _cap(request, *_):
        sent.append(_json.loads(request.content))
        return Response(201, json=_run())

    respx_mock.post(f"{_API}/api/v1/pay-runs").mock(side_effect=_cap)
    async with _client() as c:
        r = await c.post("/payroll/pay-runs/new", data={
            "frequency": "WEEKLY", "period_start": "2026-11-23",
            "period_end": "", "payment_date": "2026-11-30"})
    assert r.status_code == 303 and r.headers["location"] == f"/payroll/pay-runs/{_RUN}"
    assert sent[0]["period_end"] == "2026-11-29"


@pytest.mark.anyio
@respx.mock
async def test_create_rejects_wrong_period_length(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/pay-runs").mock(return_value=Response(201, json=_run()))
    async with _client() as c:
        r = await c.post("/payroll/pay-runs/new", data={
            "frequency": "WEEKLY", "period_start": "2026-11-23",
            "period_end": "2026-12-06", "payment_date": "2026-11-30"})
    assert r.status_code == 303 and r.headers["location"] == "/payroll/pay-runs"
    assert not route.called


def _mock_detail(rm, view):
    rm.get(f"{_API}/api/v1/pay-runs/{_RUN}/wages").mock(return_value=Response(200, json=view))
    rm.get(f"{_API}/api/v1/employees").mock(return_value=Response(200, json={
        "items": [{"id": _EMP, "employee_number": "EMP-0001", "contact_name": "Backpacker Roofer"}],
        "total": 1}))


@pytest.mark.anyio
@respx.mock
async def test_detail_shows_lines_rule_and_finalise_form(respx_mock: respx.MockRouter) -> None:
    _mock_detail(respx_mock, _view())
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}")
    assert r.status_code == 200, r.text[:400]
    html = r.text
    assert "Backpacker Roofer" in html and 'data-scale="WHM_SCH15_15"' in html
    assert "Schedule 15 working holiday makers, 15%" in html
    assert "Schedule 15" in html
    assert "Warning:" in html and "5 months" in html
    assert 'name="expected_total_net" value="1130.00"' in html
    assert 'name="expected_line_count" value="1"' in html
    assert 'name="confirm"' in html
    assert "Missing" in html  # account check panel
    assert f'action="/payroll/pay-runs/{_RUN}/lines"' in html


@pytest.mark.anyio
@respx.mock
async def test_detail_blocked_has_no_finalise_form(respx_mock: respx.MockRouter) -> None:
    _mock_detail(respx_mock, _view(can=False))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}")
    assert "cannot be finalised yet" in r.text
    assert 'name="confirm"' not in r.text
    assert "visa expired" in r.text


@pytest.mark.anyio
@respx.mock
async def test_finalised_shows_net_pay_transfers(respx_mock: respx.MockRouter) -> None:
    _mock_detail(respx_mock, _view("finalized"))
    respx_mock.get(f"{_API}/api/v1/bank_accounts").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}")
    assert "Net pay to transfer" in r.text and "062-000" in r.text
    assert f'action="/payroll/pay-runs/{_RUN}/bank-file"' in r.text
    assert "Download bank file" in r.text
    assert f'action="/payroll/pay-runs/{_RUN}/lines"' not in r.text


@pytest.mark.anyio
@respx.mock
async def test_add_line_sends_hours_and_no_items(respx_mock: respx.MockRouter) -> None:
    sent: list[dict] = []

    def _cap(request, *_):
        sent.append(_json.loads(request.content))
        return Response(201, json=_view())

    respx_mock.post(f"{_API}/api/v1/pay-runs/{_RUN}/wage-lines").mock(side_effect=_cap)
    async with _client() as c:
        r = await c.post(f"/payroll/pay-runs/{_RUN}/lines", data={
            "employee_id": _EMP, "ordinary_hours": "38", "overtime_hours": "4",
            "overtime_multiplier": "2.0"})
    assert r.status_code == 303
    assert sent == [{"employee_id": _EMP, "ordinary_hours": "38",
                     "overtime_hours": "4", "overtime_multiplier": "2.0",
                     "full_period_salary": False, "allowances": [], "deductions": [],
                     "leave_taken": [], "additional_payments": []}]


@pytest.mark.anyio
@respx.mock
async def test_finalise_needs_confirmation(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/pay-runs/{_RUN}/finalize-wages").mock(
        return_value=Response(200, json=_view("finalized")))
    form = {"version": "1", "expected_line_count": "1",
            "expected_total_net": "1130.00", "expected_total_payg": "200.00"}
    async with _client() as c:
        r = await c.post(f"/payroll/pay-runs/{_RUN}/finalise", data=form)
        assert r.status_code == 303 and not route.called
        r = await c.post(f"/payroll/pay-runs/{_RUN}/finalise", data={**form, "confirm": "yes"})
    assert r.status_code == 303 and route.called
    req = route.calls.last.request
    assert req.headers["If-Match"] == "1"
    assert _json.loads(req.content) == {"expected_line_count": 1,
                                        "expected_total_net": "1130.00",
                                        "expected_total_payg": "200.00"}


@pytest.mark.anyio
@respx.mock
async def test_payslip_has_fair_work_fields(respx_mock: respx.MockRouter) -> None:
    slip = {
        "status": "FINALIZED",
        "employer": {"name": "Sauer Pty Ltd", "legal_name": "Sauer Pty Ltd as trustee",
                     "abn": "12 345 678 901"},
        "employee": {"name": "Backpacker Roofer", "employee_number": "EMP-0001"},
        "period": {"start": "2026-11-23", "end": "2026-11-29", "payment_date": "2026-11-30"},
        "earnings": [{"description": "Ordinary hours", "hours": 38.0, "rate": 35.0,
                      "amount": 1330.0, "stp_code": "GROSS"}],
        "allowances": [], "deductions": [], "paid_leave": [], "lump_sums": [],
        "tax": {"payg": 200.0, "scale_used": "WHM_SCH15_15"},
        "super": {"amount": 159.6, "fund_name": "Test Super", "usi": "12345678901",
                  "member_number": "M1", "is_smsf": False},
        "totals": {"gross": 1330.0, "payg": 200.0, "super": 159.6, "deductions": 0.0, "net": 1130.0},
        "ytd": {"gross": 1330.0, "payg": 200.0, "super": 159.6},
        "breakdown": {},
    }
    respx_mock.get(f"{_API}/api/v1/pay-runs/{_RUN}/payslips/{_EMP}").mock(
        return_value=Response(200, json={"payslip": slip}))
    async with _client() as c:
        r = await c.get(f"/payroll/pay-runs/{_RUN}/payslips/{_EMP}")
    assert r.status_code == 200, r.text[:400]
    html = r.text
    for needle in ("Sauer Pty Ltd as trustee", "12 345 678 901", "Backpacker Roofer",
                   "2026-11-23", "2026-11-29", "2026-11-30", "38.00", "35.00",
                   "1,330.00", "200.00", "1,130.00", "159.60", "Test Super", "Payslip"):
        assert needle in html, needle
    assert "has not been finalised" not in html
    assert "Schedule 15 working holiday makers, 15%" in html
