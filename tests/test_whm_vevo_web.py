"""Working holiday maker visa / VEVO fields (engine migration 0225).

* Company settings: the WHM-employer registration fieldset renders and
  round-trips through the PATCH body.
* Employees: the visa/VEVO block renders on new/edit, is sent on create,
  present-but-blank clears to None on edit, and the detail page shows the
  VEVO state (including the "not checked" warning).
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

_COMPANY_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_EMP_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_CONTACT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

_MOCK_COMPANY = {
    "id": _COMPANY_ID,
    "name": "Acme Pty Ltd",
    "abn": "12 345 678 901",
    "version": 3,
    "archived_at": None,
}


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "TEST_SESSION_TOKEN"}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_API = settings.api_url.rstrip("/")


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()},
        follow_redirects=False,
    )


_EMPLOYEE = {
    "id": _EMP_ID,
    "company_id": _COMPANY_ID,
    "contact_id": _CONTACT_ID,
    "employee_number": "WHM01",
    "payee_id_bms": "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "tfn_masked": None,
    "tfn_status": "PROVIDED",
    "has_bank": False,
    "start_date": "2026-06-01",
    "country_code": "AU",
    "employment_basis": "C",
    "claims_tax_free_threshold": False,
    "is_australian_resident": False,
    "study_training_support_loan": False,
    "working_holiday_maker": True,
    "whm_country_code": "DE",
    "visa_subclass": "417",
    "visa_grant_number": "1234567890123",
    "visa_expiry_date": "2027-03-31",
    "vevo_checked_on": "2026-05-28",
    "vevo_work_entitlement": "LIMITED",
    "vevo_conditions": "8547 - 6 months per employer",
    "vevo_attachment_id": None,
    "income_stream_type": "WHM",
    "payslip_delivery": "EMAIL",
    "pay_frequency": "WEEKLY",
    "pay_basis": "HOURLY",
    "base_rate": "32.0000",
    "weekly_hours": "38.00",
    "version": 1,
    "created_at": "2026-11-10T00:00:00Z",
    "updated_at": "2026-11-10T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Company settings
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_settings_whm_fieldset_renders_prefilled(respx_mock: respx.MockRouter) -> None:
    company = {**_MOCK_COMPANY, "whm_employer_registered": True,
               "whm_employer_registered_on": "2026-11-01"}
    respx_mock.get(f"{_API}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )
    async with _client() as c:
        resp = await c.get("/settings/company")
    assert resp.status_code == 200
    html = resp.text
    assert 'name="whm_employer_registered"' in html
    i = html.index('id="whm_employer_registered"')
    assert "checked" in html[i:i + 300]
    assert 'value="2026-11-01"' in html


@pytest.mark.anyio
@pytest.mark.parametrize("ticked", [True, False])
async def test_settings_whm_sent_in_patch(ticked: bool) -> None:
    captured: list[dict] = []
    with respx.mock(assert_all_called=False) as rm:
        rm.get(f"{_API}/api/v1/companies").mock(
            return_value=Response(200, json={"items": [_MOCK_COMPANY], "total": 1})
        )

        def _capture(request: respx.Request, *_: object) -> Response:
            captured.append(_json.loads(request.content))
            return Response(200, json={**_MOCK_COMPANY, "version": 4})

        rm.patch(f"{_API}/api/v1/companies/{_COMPANY_ID}").mock(side_effect=_capture)
        data = {"company_id": _COMPANY_ID, "name": "Acme Pty Ltd", "version": "3"}
        if ticked:
            data["whm_employer_registered"] = "true"
            data["whm_employer_registered_on"] = "2026-11-10"
        async with _client() as c:
            resp = await c.post("/settings/company", data=data)
    assert resp.status_code == 303
    assert captured, "PATCH was not called"
    assert captured[0]["whm_employer_registered"] is ticked
    if ticked:
        assert captured[0]["whm_employer_registered_on"] == "2026-11-10"
    else:
        assert "whm_employer_registered_on" not in captured[0]


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


_WHM_FIELDS = (
    "visa_subclass",
    "visa_grant_number",
    "visa_expiry_date",
    "vevo_checked_on",
    "vevo_work_entitlement",
    "vevo_conditions",
)


def _mock_lists(rm: respx.MockRouter) -> None:
    rm.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [{"id": _CONTACT_ID, "name": "Backpacker"}], "total": 1})
    )
    rm.get(f"{_API}/api/v1/super-funds").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )


@pytest.mark.anyio
@respx.mock
async def test_new_form_renders_whm_block(respx_mock: respx.MockRouter) -> None:
    _mock_lists(respx_mock)
    async with _client() as c:
        resp = await c.get("/employees/new")
    assert resp.status_code == 200, resp.text[:500]
    for f in _WHM_FIELDS:
        assert f'name="{f}"' in resp.text, f
    assert "Working holiday makers only" in resp.text
    for v in ("UNLIMITED", "LIMITED", "NONE", "417", "462"):
        assert f'value="{v}"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_create_sends_whm_fields(respx_mock: respx.MockRouter) -> None:
    _mock_lists(respx_mock)
    captured: list[dict] = []

    def _capture(request: respx.Request, *_: object) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json={**_EMPLOYEE})

    respx_mock.post(f"{_API}/api/v1/employees").mock(side_effect=_capture)
    form = {
        "contact_id": _CONTACT_ID,
        "start_date": "2026-06-01",
        "employment_basis": "C",
        "base_rate": "32.00",
        "working_holiday_maker": "on",
        "whm_country_code": "DE",
        "visa_subclass": "417",
        "visa_grant_number": "1234567890123",
        "visa_expiry_date": "2027-03-31",
        "vevo_checked_on": "2026-05-28",
        "vevo_work_entitlement": "LIMITED",
        "vevo_conditions": "8547 - 6 months per employer",
    }
    async with _client() as c:
        resp = await c.post("/employees/new", data=form)
    assert resp.status_code == 303, resp.text[:500]
    body = captured[0]
    for f in _WHM_FIELDS:
        assert body[f] == form[f], f


@pytest.mark.anyio
@respx.mock
async def test_create_omits_blank_whm_fields(respx_mock: respx.MockRouter) -> None:
    _mock_lists(respx_mock)
    captured: list[dict] = []

    def _capture(request: respx.Request, *_: object) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json={**_EMPLOYEE})

    respx_mock.post(f"{_API}/api/v1/employees").mock(side_effect=_capture)
    form = {"contact_id": _CONTACT_ID, "start_date": "2026-06-01",
            "employment_basis": "F", "base_rate": "35.00"}
    form.update({f: "" for f in _WHM_FIELDS})
    async with _client() as c:
        resp = await c.post("/employees/new", data=form)
    assert resp.status_code == 303
    for f in _WHM_FIELDS:
        assert f not in captured[0], f


@pytest.mark.anyio
@respx.mock
async def test_edit_sends_values_and_blank_clears(respx_mock: respx.MockRouter) -> None:
    captured: list[dict] = []

    def _capture(request: respx.Request, *_: object) -> Response:
        captured.append(_json.loads(request.content))
        return Response(200, json={**_EMPLOYEE, "version": 2})

    respx_mock.patch(f"{_API}/api/v1/employees/{_EMP_ID}").mock(side_effect=_capture)
    form = {
        "version": "1",
        "visa_subclass": "462",
        "visa_grant_number": "",
        "visa_expiry_date": "2027-05-01",
        "vevo_checked_on": "2026-11-01",
        "vevo_work_entitlement": "UNLIMITED",
        "vevo_conditions": "",
    }
    async with _client() as c:
        resp = await c.post(f"/employees/{_EMP_ID}/edit", data=form)
    assert resp.status_code == 303, resp.text[:500]
    body = captured[0]
    assert body["visa_subclass"] == "462"
    assert body["visa_expiry_date"] == "2027-05-01"
    assert body["vevo_checked_on"] == "2026-11-01"
    assert body["vevo_work_entitlement"] == "UNLIMITED"
    assert body["visa_grant_number"] is None
    assert body["vevo_conditions"] is None


@pytest.mark.anyio
@respx.mock
async def test_edit_form_prefills_whm_block(respx_mock: respx.MockRouter) -> None:
    _mock_lists(respx_mock)
    respx_mock.get(f"{_API}/api/v1/employees/{_EMP_ID}").mock(
        return_value=Response(200, json=_EMPLOYEE)
    )
    async with _client() as c:
        resp = await c.get(f"/employees/{_EMP_ID}/edit")
    assert resp.status_code == 200
    html = resp.text
    assert 'value="2027-03-31"' in html
    assert 'value="2026-05-28"' in html
    assert 'value="LIMITED" selected' in html
    assert 'value="417" selected' in html
    assert "8547 - 6 months per employer</textarea>" in html


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides,expect",
    [
        ({}, ["Limited work rights", "checked 2026-05-28", "8547 - 6 months per employer",
              "expires 2027-03-31"]),
        ({"vevo_checked_on": None, "vevo_work_entitlement": None},
         ["Not checked. Pay runs will refuse this employee"]),
        ({"vevo_work_entitlement": "NONE"}, ["No work rights"]),
    ],
)
async def test_detail_shows_vevo_state(overrides: dict, expect: list[str]) -> None:
    with respx.mock(assert_all_called=False) as rm:
        rm.get(f"{_API}/api/v1/employees/{_EMP_ID}").mock(
            return_value=Response(200, json={**_EMPLOYEE, **overrides})
        )
        rm.get(f"{_API}/api/v1/contacts/{_CONTACT_ID}").mock(
            return_value=Response(200, json={"id": _CONTACT_ID, "name": "Backpacker"})
        )
        async with _client() as c:
            resp = await c.get(f"/employees/{_EMP_ID}")
    assert resp.status_code == 200, resp.text[:500]
    for e in expect:
        assert e in resp.text, e


@pytest.mark.anyio
@respx.mock
async def test_detail_non_whm_has_no_vevo_block(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/employees/{_EMP_ID}").mock(
        return_value=Response(200, json={**_EMPLOYEE, "working_holiday_maker": False})
    )
    respx_mock.get(f"{_API}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json={"id": _CONTACT_ID, "name": "Backpacker"})
    )
    async with _client() as c:
        resp = await c.get(f"/employees/{_EMP_ID}")
    assert resp.status_code == 200
    assert "VEVO" not in resp.text
