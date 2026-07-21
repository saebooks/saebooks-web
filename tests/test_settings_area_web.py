"""Tests for the reworked Settings area.

Covers the new hub, the api-tokens / users / preferences surfaces, the
mode-aware company form, the tax_registered round-trip bug fix, and the
fin-year day/month cross-validation error rendering.
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

_FULL_COMPANY = {
    "id": _COMPANY_ID,
    "name": "Acme Pty Ltd",
    "legal_name": "Acme Proprietary Limited",
    "trading_name": "Acme",
    "abn": "12 345 678 901",
    "acn": "123 456 789",
    "base_currency": "AUD",
    "bookkeeping_mode": "full",
    "tax_registered": True,
    "gst_effective_date": "2020-07-01",
    "fin_year_start_month": 7,
    "fin_year_start_day": 1,
    "phone": "+61 7 5555 1234",
    "email": "hello@acme.example",
    "website": "https://acme.example",
    "bank_bsb": "123-456",
    "costing_method": "weighted_average",
    "ar_control_account_code": "",
    "address": {"line1": "Level 1", "city": "Brisbane", "state": "QLD", "postcode": "4000", "country": "Australia"},
    "version": 3,
    "archived_at": None,
}

_CASHBOOK_COMPANY = {**_FULL_COMPANY, "bookkeeping_mode": "cashbook"}


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
        follow_redirects=False,
    )


def _mock_companies(respx_mock, company=_FULL_COMPANY):
    """Middleware + page both hit /api/v1/companies."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_settings_hub_renders_groups(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    async with _client() as client:
        resp = await client.get("/settings")
    assert resp.status_code == 200
    # Grouped, coherent area — the six-group structure is visible.
    for heading in ("Organisation", "Users &amp; API access", "Preferences"):
        assert heading in resp.text
    # Cards link to the real working surfaces (nothing dead).
    for href in ("/settings/company", "/settings/api-tokens", "/settings/users", "/settings/preferences", "/profile"):
        assert href in resp.text


@pytest.mark.anyio
@respx.mock
async def test_settings_hub_full_mode_shows_tax_and_banking(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _FULL_COMPANY)
    async with _client() as client:
        resp = await client.get("/settings")
    # Unique hub-card copy (raw hrefs also appear in the top-bar Create menu).
    assert "The GST / tax rates applied" in resp.text
    assert "The accounts you reconcile" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_settings_hub_cashbook_mode_hides_tax_and_banking(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _CASHBOOK_COMPANY)
    async with _client() as client:
        resp = await client.get("/settings")
    assert resp.status_code == 200
    # The Financial-year-&-tax and Banking hub groups are full-accounting only.
    assert "Financial year &amp; tax" not in resp.text
    assert "The GST / tax rates applied" not in resp.text
    assert "The accounts you reconcile" not in resp.text


# ---------------------------------------------------------------------------
# Company form — new fields + mode awareness
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_form_renders_new_engine_fields(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _FULL_COMPANY)
    async with _client() as client:
        resp = await client.get("/settings/company")
    assert resp.status_code == 200
    for name in ("acn", "phone", "email", "website", "bank_bsb", "bank_account_name",
                 "default_payment_terms", "terms_url", "costing_method",
                 "ar_control_account_code", "lifecycle_status"):
        assert f'name="{name}"' in resp.text, f"missing field {name}"
    # base_currency shown read-only (disabled), not an editable field.
    assert "AUD" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_company_form_cashbook_hides_accrual_only_fields(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _CASHBOOK_COMPANY)
    async with _client() as client:
        resp = await client.get("/settings/company")
    assert resp.status_code == 200
    # Accrual-only policy is hidden for a single-entry (cashbook) company.
    assert 'name="costing_method"' not in resp.text
    assert 'name="psi_status"' not in resp.text
    assert 'name="ar_control_account_code"' not in resp.text


# ---------------------------------------------------------------------------
# tax_registered round-trip (the bug: form used non-existent gst_registered)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_registered_prefills_checkbox(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _FULL_COMPANY)  # tax_registered=True
    async with _client() as client:
        resp = await client.get("/settings/company")
    assert 'name="tax_registered"' in resp.text
    # The checkbox reflects the engine's tax_registered=True.
    checkbox = resp.text.split('name="tax_registered"', 1)[1][:120]
    assert "checked" in checkbox


@pytest.mark.anyio
@respx.mock
async def test_tax_registered_sent_in_patch_payload(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json=_FULL_COMPANY)
    )
    _mock_companies(respx_mock, _FULL_COMPANY)
    async with _client() as client:
        resp = await client.post(
            "/settings/company",
            data={"company_id": _COMPANY_ID, "name": "Acme Pty Ltd", "version": "3",
                  "tax_registered": "true", "bookkeeping_mode": "full"},
        )
    assert resp.status_code == 303
    body = _json.loads(route.calls[-1].request.content)
    # The engine field is tax_registered, NOT the old bogus gst_registered.
    assert body.get("tax_registered") is True
    assert "gst_registered" not in body


@pytest.mark.anyio
@respx.mock
async def test_tax_registered_unchecked_sends_false(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json=_FULL_COMPANY)
    )
    _mock_companies(respx_mock, _FULL_COMPANY)
    async with _client() as client:
        resp = await client.post(
            "/settings/company",
            data={"company_id": _COMPANY_ID, "name": "Acme Pty Ltd", "version": "3",
                  "bookkeeping_mode": "full"},  # checkbox absent = unchecked
        )
    assert resp.status_code == 303
    body = _json.loads(route.calls[-1].request.content)
    assert body.get("tax_registered") is False


# ---------------------------------------------------------------------------
# fin-year day/month cross-validation error renders readably
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_fin_year_day_validation_error_renders(respx_mock: respx.MockRouter) -> None:
    msg = "Day 31 is not valid for the selected start month."
    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(422, json={"detail": [
            {"loc": ["body", "fin_year_start_day"], "msg": msg, "type": "value_error"}
        ]})
    )
    _mock_companies(respx_mock, _FULL_COMPANY)  # has fin_year_start_day -> field enabled
    async with _client() as client:
        resp = await client.post(
            "/settings/company",
            data={"company_id": _COMPANY_ID, "name": "Acme Pty Ltd", "version": "3",
                  "fin_year_start_month": "2", "fin_year_start_day": "31",
                  "bookkeeping_mode": "full"},
        )
    assert resp.status_code == 422
    # The per-field cross-validation message is surfaced on the page.
    assert msg in resp.text


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_api_tokens_list_renders(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/api-tokens").mock(
        return_value=Response(200, json=[
            {"id": "t1", "name": "Laptop CLI", "token_prefix": "ab12cd", "active": True,
             "last_used_at": None, "revoked_at": None},
        ])
    )
    async with _client() as client:
        resp = await client.get("/settings/api-tokens")
    assert resp.status_code == 200
    assert "Laptop CLI" in resp.text
    assert "Create a token" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_api_tokens_degrades_on_404(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/api-tokens").mock(return_value=Response(404))
    async with _client() as client:
        resp = await client.get("/settings/api-tokens")
    assert resp.status_code == 200
    # M2 module-degrade banner, not a blank/broken page.
    assert "available on your edition" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_api_tokens_create_calls_engine(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    route = respx_mock.post(f"{_API_BASE}/api/v1/api-tokens").mock(
        return_value=Response(201, json={"name": "CI", "token": "saebk_secretvalue", "token_prefix": "se12cr"})
    )
    async with _client() as client:
        resp = await client.post("/settings/api-tokens", data={"name": "CI"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/api-tokens"
    body = _json.loads(route.calls[-1].request.content)
    assert body.get("name") == "CI"


@pytest.mark.anyio
@respx.mock
async def test_api_tokens_create_requires_name(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/api-tokens").mock(return_value=Response(200, json=[]))
    async with _client() as client:
        resp = await client.post("/settings/api-tokens", data={"name": "  "})
    assert resp.status_code == 422
    assert "recognise it later" in resp.text


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_users_list_renders(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/users").mock(
        return_value=Response(200, json={"items": [
            {"id": "u1", "display_name": "Sam Demo", "username": "sam", "email": "sam@example.com", "role": "admin"},
        ]})
    )
    async with _client() as client:
        resp = await client.get("/settings/users")
    assert resp.status_code == 200
    assert "Sam Demo" in resp.text
    assert "sam@example.com" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_users_degrades_on_404(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/users").mock(return_value=Response(404))
    async with _client() as client:
        resp = await client.get("/settings/users")
    assert resp.status_code == 200
    assert "available on your edition" in resp.text


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_preferences_renders(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock)
    async with _client() as client:
        resp = await client.get("/settings/preferences")
    assert resp.status_code == 200
    assert "Language" in resp.text
    assert "Appearance" in resp.text
    assert "Bookkeeping mode" in resp.text
