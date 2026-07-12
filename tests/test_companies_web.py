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

Fixer round 4:
14. GET /companies/new -- stock SAE Books brand renders no "Jurisdiction"
    select and no EE fieldset markup at all (not just hidden) -- the
    affordance must be brand-gated like every other EE-only UI surface.
15. GET /companies/new -- Tasur brand DOES render the jurisdiction select.
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
# EE onboarding (P3 GUI + Packet 2 wiring)
#
# Packet 2: the engine's CompanyCreate now accepts jurisdiction/
# registrikood/kmv_number/coa_template_key for real (verified against
# saebooks/api/v1/schemas.py in the engine repo) and implements the
# ee/default chart applier. These tests mock the engine returning 201/422
# and assert the exact payload this route sends -- note the form's `kmv`
# field is posted to the engine as `kmv_number`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. POST /companies jurisdiction=EE -- valid -> 201, payload wired, plain
#    success flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_success_wires_payload(respx_mock: respx.MockRouter) -> None:
    """Valid EE submission -> 201; engine payload carries the EE fields
    under the engine's own field names; redirect flash is the plain
    success message, same as AU."""
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
        assert sent["kmv_number"] == "EE123456789"  # normalised to uppercase
        assert sent["base_currency"] == "EUR"
        assert sent["coa_template_key"] == "ee/default"
        assert "abn" not in sent
        assert "kmv" not in sent  # form field name, not the engine's

        # Follow the redirect (reusing the same client so the updated
        # session cookie from the POST's Set-Cookie carries the flash).
        respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
            return_value=Response(200, json=_MOCK_COMPANIES)
        )
        respx_mock.get(f"{_API_BASE}/api/v1/license").mock(
            return_value=Response(200, json=_LICENSE_ENTERPRISE)
        )
        resp2 = await client.get(resp.headers["location"])
        assert "Company created." in resp2.text


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
    assert "kmv_number" not in captured[0]
    assert "kmv" not in captured[0]


# ---------------------------------------------------------------------------
# 12b. POST /companies jurisdiction=EE -- engine-side 422 (e.g. a
#      registrikood the client regex passed but the engine still rejects)
#      renders inline on the form, mapped back to the `kmv` field name.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_companies_create_ee_engine_422_renders_on_form(respx_mock: respx.MockRouter) -> None:
    """A 422 from the engine (not caught by client-side validation) is
    surfaced inline on the form rather than a bare API-error message, and
    the engine's `kmv_number` field name maps back to this form's `kmv`
    field for the error to land next to the right input.

    Fixer round 4: mocked in the engine's REAL response shape, not a
    fabricated one. api_client() uses a bare httpx.AsyncClient, which sends
    "Accept: */*" by default -- that satisfies the engine's RFC 7807
    _wants_json check (saebooks/api/errors.py), so field-scoped pydantic
    errors live under top-level "errors", and "detail" is always a fixed
    human string, never the per-field list."""
    respx_mock.post(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(
            422,
            json={
                "type": "https://saebooks.io/problems/validation_failed",
                "title": "Validation Failed",
                "status": 422,
                "code": "validation_failed",
                "detail": "Request body or query parameters failed validation.",
                "errors": [
                    {
                        "loc": ["body", "kmv_number"],
                        "msg": "kmv_number must be 'EE' followed by 9 digits",
                        "type": "value_error",
                    }
                ],
            },
        )
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
                "kmv": "EE123456789",
            },
        )

    assert resp.status_code == 422
    assert "kmv_number must be" in resp.text
    assert 'id="kmv"' in resp.text


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


@pytest.mark.anyio
@respx.mock
async def test_companies_create_blank_jurisdiction_falls_back_to_au(
    respx_mock: respx.MockRouter,
) -> None:
    """A present-but-whitespace jurisdiction value (e.g. a stale form
    re-render) normalizes to AU rather than "" -- "" fails the == "EE"
    check and used to fall through to the AU path silently dropping any
    registrikood/kmv the caller also sent (critic round 1, finding 7).
    The AU-path payload never carries a jurisdiction key (matches
    test_companies_create_au_payload_unchanged), so this also proves the
    EE-only fields didn't leak through."""
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
            data={"name": "Apex Fitness", "jurisdiction": "  ", "registrikood": "12345678"},
        )

    assert resp.status_code == 303
    assert captured[0] == {"name": "Apex Fitness"}


# ---------------------------------------------------------------------------
# 14-15. Fixer round 4 -- jurisdiction selector / EE fieldset brand-gated
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_companies_new_form_stock_brand_hides_jurisdiction_selector() -> None:
    """GET /companies/new on the stock (default, SAEBOOKS_BRAND unset) SAE
    Books brand must not render the EE-onboarding jurisdiction select or
    the EE fieldset at all -- not even as hidden markup. AU pixel
    equivalence means the default deployment gains no new form field."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/companies/new")

    assert resp.status_code == 200
    assert 'id="jurisdiction"' not in resp.text
    assert 'id="registrikood"' not in resp.text
    assert 'id="ee-fields"' not in resp.text
    # AU's own field is unaffected.
    assert 'name="abn"' in resp.text


@pytest.mark.anyio
async def test_companies_new_form_tasur_brand_shows_jurisdiction_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same route under the Tasur/EE brand DOES render the selector -- the
    gate hides it for stock only, it doesn't remove the feature."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _ADMIN_COOKIE},
    ) as client:
        resp = await client.get("/companies/new")

    assert resp.status_code == 200
    assert 'id="jurisdiction"' in resp.text
    assert 'id="registrikood"' in resp.text
