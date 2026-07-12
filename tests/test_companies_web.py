"""Tests for company management web routes — FITC-1 gap fix.

Covers:
1. GET /companies            -- list renders, shows company rows
2. GET /companies/new        -- form renders for admin
3. GET /companies/new        -- 403 for non-admin
4. POST /companies           -- 201 from API -> 303 redirect
5. POST /companies           -- 404 from API (flag disabled) -> 403 page
6. GET /settings/companies   -- redirects to /companies
7. GET /admin/license        -- renders edition + flags table
8. GET /admin/license        -- 403 for non-admin

EE onboarding (P3, ee-gui-prep scope):
9.  POST /companies jurisdiction=EE -- valid registrikood/kmv -> 201,
    payload sent to the (mocked) engine includes jurisdiction/registrikood/
    kmv/base_currency/coa_template_key, flash carries the honest
    not-yet-persisted caveat (prerequisite gap: verified against the
    engine's CompanyCreate schema, which does not accept these fields yet).
10. POST /companies jurisdiction=EE -- missing registrikood -> 422, no
    engine call made.
11. POST /companies jurisdiction=EE -- malformed registrikood (not 8
    digits) -> 422 with a field error.
12. POST /companies jurisdiction=EE -- malformed kmv -> 422 with a field
    error; registrikood alone (kmv omitted) is valid (kmv is optional).
13. POST /companies jurisdiction=AU (default/unset) -- payload sent to the
    engine is byte-identical to the pre-P3 shape (no jurisdiction key at
    all) -- AU path stays pixel/behaviour-equivalent.
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

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")

_COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_MOCK_COMPANY = {
    "id": _COMPANY_ID,
    "name": "Apex Fitness",
    "legal_name": "Apex Fitness Pty Ltd",
    "trading_name": "Apex",
    "abn": "11 111 111 111",
    "base_currency": "AUD",
    "version": 1,
    "archived_at": None,
}
_MOCK_COMPANIES = {"items": [_MOCK_COMPANY], "total": 1}

_LICENSE_ENTERPRISE = {
    "edition": "enterprise",
    "flags": {"multi_company": True, "bank_feeds": True},
    "all_flags": ["multi_company", "bank_feeds"],
    "tier_order": ["community", "offline", "business", "pro", "enterprise"],
}
_LICENSE_COMMUNITY = {
    "edition": "community",
    "flags": {"multi_company": False, "bank_feeds": False},
    "all_flags": ["multi_company", "bank_feeds"],
    "tier_order": ["community", "offline", "business", "pro", "enterprise"],
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_ADMIN_COOKIE = _make_session_cookie(
    {"api_token": "test-token-admin", "user_role": "admin", "is_sae_staff": False}
)
_BOOKKEEPER_COOKIE = _make_session_cookie(
    {"api_token": "test-token-bk", "user_role": "bookkeeper", "is_sae_staff": False}
)
_NO_AUTH_COOKIE = _make_session_cookie({})


# ---------------------------------------------------------------------------
# 1. GET /companies — list renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_list_renders(respx_mock: respx.MockRouter) -> None:
    """GET /companies returns 200 with the company name."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_MOCK_COMPANIES)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        return_value=Response(200, json=_LICENSE_ENTERPRISE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/companies")

    assert resp.status_code == 200
    assert "Apex Fitness" in resp.text
    assert "11 111 111 111" in resp.text


# ---------------------------------------------------------------------------
# 2. GET /companies/new — form renders for admin
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_companies_new_form_admin() -> None:
    """GET /companies/new returns 200 with name input for an admin."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/companies/new")

    assert resp.status_code == 200
    assert 'name="name"' in resp.text


# ---------------------------------------------------------------------------
# 3. GET /companies/new — 403 for bookkeeper
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_companies_new_form_forbidden_for_bookkeeper() -> None:
    """GET /companies/new returns 403 for a non-admin user."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE},
    ) as client:
        resp = await client.get("/companies/new")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. POST /companies — 201 from API -> 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_success(respx_mock: respx.MockRouter) -> None:
    """POST /companies with valid payload; API 201 -> 303 to /companies."""
    respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(201, json=_MOCK_COMPANY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/companies",
            data={"name": "Apex Fitness", "abn": "11 111 111 111"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/companies"


# ---------------------------------------------------------------------------
# 5. POST /companies — 404 from API (flag disabled) -> 403 page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_flag_disabled(respx_mock: respx.MockRouter) -> None:
    """POST /companies when multi-company is disabled; API 404 -> 403 in UI."""
    respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(404, json={"detail": "Not Found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.post(
            "/companies",
            data={"name": "Apex Fitness"},
        )

    assert resp.status_code == 403
    assert "Multi-company" in resp.text


# ---------------------------------------------------------------------------
# 6. GET /settings/companies — redirects to /companies
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_settings_companies_redirect() -> None:
    """GET /settings/companies returns 302 redirect to /companies."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/settings/companies")

    assert resp.status_code == 302
    assert "/companies" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 7. GET /admin/license — renders edition + flags
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_admin_license_renders(respx_mock: respx.MockRouter) -> None:
    """GET /admin/license returns 200 with edition and multi_company flag."""
    respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
        return_value=Response(200, json=_LICENSE_ENTERPRISE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/admin/license")

    assert resp.status_code == 200
    assert "enterprise" in resp.text
    assert "multi_company" in resp.text


# ---------------------------------------------------------------------------
# 8. GET /admin/license — 403 for bookkeeper
# ---------------------------------------------------------------------------



@pytest.mark.anyio
async def test_admin_license_forbidden_for_bookkeeper() -> None:
    """GET /admin/license returns 403 for a non-admin user."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE},
    ) as client:
        resp = await client.get("/admin/license")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# EE onboarding (P3, ee-gui-prep scope)
#
# Prerequisite gap verified against the engine (saebooks/api/v1/schemas.py,
# CompanyCreate): jurisdiction/registrikood/kmv/coa_template_key are not
# accepted there today. These tests mock the engine returning 201 as if it
# already accepted them (real wiring, future-ready) -- see companies.py's
# module-level note and the flash caveat asserted in test 9 below, which is
# the honest signal that these values are not actually persisted yet.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. POST /companies jurisdiction=EE -- valid -> 201, payload wired, honest
#    flash caveat
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_success_wires_payload(respx_mock: respx.MockRouter) -> None:
    """Valid EE submission -> 201; engine payload carries the new EE
    fields (real wiring, not faked); redirect flash is the honest
    not-yet-persisted caveat, not a bare "Company created."."""
    captured: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json=_MOCK_COMPANY)

    respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/companies",
            data={
                "name": "Tasur OÜ",
                "jurisdiction": "EE",
                "registrikood": "12345678",
                "kmv": "ee123456789",
                "base_currency": "EUR",
                "coa_template_key": "ee/default",
            },
        )

        assert resp.status_code == 303
        assert len(captured) == 1, "Expected exactly one upstream POST call"
        sent = captured[0]
        assert sent["jurisdiction"] == "EE"
        assert sent["registrikood"] == "12345678"
        assert sent["kmv"] == "EE123456789"  # normalised to uppercase
        assert sent["base_currency"] == "EUR"
        assert sent["coa_template_key"] == "ee/default"
        assert "abn" not in sent

        # Follow the redirect (reusing the same client so the updated
        # session cookie from the POST's Set-Cookie carries the flash)
        # and check it renders the honest caveat rather than papering
        # over the unmet engine prerequisite.
        respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
            return_value=Response(200, json=_MOCK_COMPANIES)
        )
        respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
            return_value=Response(200, json=_LICENSE_ENTERPRISE)
        )
        resp2 = await client.get(resp.headers["location"])
        assert "could not be saved" in resp2.text
        assert "not persisted" in resp2.text


# ---------------------------------------------------------------------------
# 10. POST /companies jurisdiction=EE -- missing registrikood -> 422, no
#     engine call
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_missing_registrikood(respx_mock: respx.MockRouter) -> None:
    """EE submission with no registrikood -> 422, server-side validation
    rejects before any upstream call is made."""
    route = respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(201, json=_MOCK_COMPANY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.post(
            "/companies",
            data={"name": "Tasur OÜ", "jurisdiction": "EE"},
        )

    assert resp.status_code == 422
    assert "required" in resp.text.lower()
    assert route.call_count == 0, "Should not call the engine when validation fails"


# ---------------------------------------------------------------------------
# 11. POST /companies jurisdiction=EE -- malformed registrikood -> 422
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_malformed_registrikood(respx_mock: respx.MockRouter) -> None:
    """registrikood that isn't exactly 8 digits -> 422 field error."""
    route = respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(201, json=_MOCK_COMPANY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.post(
            "/companies",
            data={"name": "Tasur OÜ", "jurisdiction": "EE", "registrikood": "123"},
        )

    assert resp.status_code == 422
    assert "8 digits" in resp.text
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# 12. POST /companies jurisdiction=EE -- malformed kmv -> 422; kmv omitted
#     is valid (optional)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_malformed_kmv(respx_mock: respx.MockRouter) -> None:
    """kmv that doesn't match EE + 9 digits -> 422 field error."""
    route = respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(201, json=_MOCK_COMPANY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.post(
            "/companies",
            data={
                "name": "Tasur OÜ",
                "jurisdiction": "EE",
                "registrikood": "12345678",
                "kmv": "DE123456789",
            },
        )

    assert resp.status_code == 422
    assert "EE" in resp.text
    assert route.call_count == 0


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_kmv_optional(respx_mock: respx.MockRouter) -> None:
    """kmv omitted entirely is valid -- 201, and kmv is absent from the
    engine payload rather than sent as an empty string."""
    captured: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json=_MOCK_COMPANY)

    respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/companies",
            data={"name": "Tasur OÜ", "jurisdiction": "EE", "registrikood": "12345678"},
        )

    assert resp.status_code == 303
    assert "kmv" not in captured[0]


# ---------------------------------------------------------------------------
# 13. POST /companies jurisdiction=AU (default/unset) -- payload unchanged
#     from the pre-P3 shape
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_au_payload_unchanged(respx_mock: respx.MockRouter) -> None:
    """AU submission (no jurisdiction field, matching the pre-P3 form)
    sends the exact same payload shape as before -- no jurisdiction,
    registrikood, kmv, base_currency or coa_template_key keys leak in."""
    captured: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json=_MOCK_COMPANY)

    respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/companies",
            data={"name": "Apex Fitness", "abn": "11 111 111 111"},
        )

    assert resp.status_code == 303
    assert captured[0] == {"name": "Apex Fitness", "abn": "11 111 111 111"}
