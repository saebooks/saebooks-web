"""Tests for the company settings' financial-year fields (period picker work).

1. test_form_prefills_fin_year_start_month       — GET renders the select
   pre-selected to the company's stored month.
2. test_form_defaults_to_july_when_unset         — GET with no stored value
   defaults the select to July (AU default), matching the engine default.
3. test_day_input_locked_when_engine_field_absent — the day <input> renders
   disabled (and is NOT itself POSTed) when the company payload has no
   fin_year_start_day key — the expected state until the engine spec lands.
4. test_day_input_unlocked_when_engine_field_present — once a company
   payload DOES carry fin_year_start_day, the input renders enabled.
5. test_post_sends_fin_year_start_month_not_day  — submitting the form (day
   locked) PATCHes fin_year_start_month but never fin_year_start_day.
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

_MOCK_COMPANY = {
    "id": _COMPANY_ID,
    "name": "Acme Pty Ltd",
    "legal_name": "Acme Proprietary Limited",
    "trading_name": "Acme",
    "abn": "12 345 678 901",
    "address": None,
    "version": 3,
    "archived_at": None,
    "fin_year_start_month": 1,
}

_MOCK_COMPANIES = {"items": [_MOCK_COMPANY], "total": 1}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "TEST_SESSION_TOKEN"})
_API_BASE = settings.api_url.rstrip("/")


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


@pytest.mark.anyio
@respx.mock
async def test_form_prefills_fin_year_start_month(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_MOCK_COMPANIES)
    )

    async with _client() as client:
        resp = await client.get("/settings/company")

    assert resp.status_code == 200
    # January (value=1) selected, matching the mock company.
    assert '<option value="1" selected>' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_form_defaults_to_july_when_unset(respx_mock: respx.MockRouter) -> None:
    company = {**_MOCK_COMPANY}
    del company["fin_year_start_month"]
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )

    async with _client() as client:
        resp = await client.get("/settings/company")

    assert resp.status_code == 200
    assert '<option value="7" selected>' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_day_input_locked_when_engine_field_absent(
    respx_mock: respx.MockRouter,
) -> None:
    """No fin_year_start_day on the company payload -> the day input is
    disabled and the "coming soon" note is shown."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_MOCK_COMPANIES)
    )

    async with _client() as client:
        resp = await client.get("/settings/company")

    assert resp.status_code == 200
    assert 'id="fin_year_start_day"' in resp.text
    assert "disabled" in resp.text
    assert "Coming soon" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_day_input_unlocked_when_engine_field_present(
    respx_mock: respx.MockRouter,
) -> None:
    """Once the engine starts returning fin_year_start_day, the web form
    lights it up automatically — no code change needed on this side."""
    company = {**_MOCK_COMPANY, "fin_year_start_day": 15}
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )

    async with _client() as client:
        resp = await client.get("/settings/company")

    assert resp.status_code == 200
    assert 'name="fin_year_start_day"' in resp.text
    assert 'value="15"' in resp.text
    # The enabled variant has no "disabled" attribute on that specific input.
    day_input_start = resp.text.index('id="fin_year_start_day"')
    day_input_end = resp.text.index(">", day_input_start)
    assert "disabled" not in resp.text[day_input_start:day_input_end]


@pytest.mark.anyio
@respx.mock
async def test_post_sends_fin_year_start_month_not_day(
    respx_mock: respx.MockRouter,
) -> None:
    """POST with only fin_year_start_month in the form (day locked/disabled,
    so a real browser never submits it) PATCHes month only."""
    captured: dict = {}

    def _capture_patch(request):
        captured["body"] = _json.loads(request.content)
        return Response(200, json=_MOCK_COMPANY)

    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        side_effect=_capture_patch
    )

    async with _client() as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",
                "fin_year_start_month": "1",
            },
        )

    assert resp.status_code == 303
    assert captured["body"].get("fin_year_start_month") == 1
    assert "fin_year_start_day" not in captured["body"]
