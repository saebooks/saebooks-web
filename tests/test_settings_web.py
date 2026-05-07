"""Tests for the company settings page — Lane D.

Four tests:
1. test_company_settings_form_renders  — GET /settings/company → 200, form fields present
2. test_company_settings_update_success — POST form data, API PATCH 200 → 303 redirect
3. test_company_settings_update_409    — POST form data, API PATCH 409 → 200 with conflict message
4. test_company_settings_nav_link      — GET /contacts → 200, Settings nav link present
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

_COMPANY_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_MOCK_COMPANY = {
    "id": _COMPANY_ID,
    "name": "Acme Pty Ltd",
    "legal_name": "Acme Proprietary Limited",
    "trading_name": "Acme",
    "abn": "12 345 678 901",
    "address": {
        "line1": "Level 1, 123 Main St",
        "line2": None,
        "city": "Brisbane",
        "state": "QLD",
        "postcode": "4000",
        "country": "Australia",
    },
    "version": 3,
    "archived_at": None,
}

_MOCK_COMPANIES = {"items": [_MOCK_COMPANY], "total": 1}
_MOCK_COMPANIES_V4 = {"items": [{**_MOCK_COMPANY, "version": 4, "name": "Acme Pty Ltd (server)"}], "total": 1}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "TEST_SESSION_TOKEN"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. GET /settings/company — form renders with key fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_settings_form_renders(respx_mock: respx.MockRouter) -> None:
    """GET /settings/company returns 200 with company name, ABN, and hidden version."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=_MOCK_COMPANIES)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/settings/company")

    assert resp.status_code == 200
    # Required fields present.
    assert 'name="name"' in resp.text
    assert 'name="abn"' in resp.text
    # Address sub-fields present.
    assert 'name="address_line1"' in resp.text
    assert 'name="address_city"' in resp.text
    assert 'name="address_postcode"' in resp.text
    # Hidden version field populated.
    assert 'name="version"' in resp.text
    assert 'value="3"' in resp.text
    # Pre-filled values from the mock company.
    assert "Acme Pty Ltd" in resp.text
    assert "12 345 678 901" in resp.text


# ---------------------------------------------------------------------------
# 2. POST /settings/company — success → 303 redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_settings_update_success(respx_mock: respx.MockRouter) -> None:
    """POST /settings/company with valid data; API PATCH 200 → 303 to /settings/company."""
    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json=_MOCK_COMPANY)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "legal_name": "Acme Proprietary Limited",
                "trading_name": "Acme",
                "abn": "12 345 678 901",
                "version": "3",
                "address_line1": "Level 1, 123 Main St",
                "address_city": "Brisbane",
                "address_state": "QLD",
                "address_postcode": "4000",
                "address_country": "Australia",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/settings/company?company_id={_COMPANY_ID}"


# ---------------------------------------------------------------------------
# 3. POST /settings/company — API 409 → 200 with conflict message
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_settings_update_409(respx_mock: respx.MockRouter) -> None:
    """POST /settings/company with stale version; API 409 → 200 with conflict message in body."""
    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(409, json={"detail": "Version conflict"})
    )
    # The route re-fetches the company after a 409 to obtain the latest version.
    respx_mock.get(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json={**_MOCK_COMPANY, "version": 4})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",  # stale
            },
        )

    assert resp.status_code == 409
    # Conflict message visible.
    assert "conflict-banner" in resp.text
    assert "Someone else has updated this company record" in resp.text
    # Server's version (4) applied to the hidden input.
    assert 'value="4"' in resp.text


# ---------------------------------------------------------------------------
# 4. GET /contacts — Settings nav link present
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_settings_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /contacts renders page with a Settings link pointing to /settings/company."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/contacts")

    assert resp.status_code == 200
    # Nav link to the settings page is rendered (case-insensitive check).
    assert "/settings/company" in resp.text
    assert "settings" in resp.text.lower()


# ---------------------------------------------------------------------------
# 5. HOBB-5 — GST backdating confirmation workflow
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_settings_gst_backdate_shows_confirm(respx_mock: respx.MockRouter) -> None:
    """POST with a backdated gst_effective_date triggers the confirmation page."""
    from datetime import date, timedelta

    old_date = (date.today() - timedelta(days=60)).isoformat()

    respx_mock.get(
        f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}/gst-backdate-preview"
    ).mock(
        return_value=Response(200, json={"invoice_count": 3, "effective_date": old_date})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",
                "gst_registered": "true",
                "gst_effective_date": old_date,
            },
        )

    assert resp.status_code == 200
    assert "Confirm" in resp.text
    assert "3 invoice" in resp.text
    assert old_date in resp.text
    assert 'name="backdate_confirmed" value="true"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_company_settings_gst_backdate_confirmed_saves(respx_mock: respx.MockRouter) -> None:
    """POST with backdate_confirmed=true proceeds to save and 303 redirects."""
    from datetime import date, timedelta

    old_date = (date.today() - timedelta(days=60)).isoformat()
    updated_company = {**_MOCK_COMPANY, "gst_registered": True, "gst_effective_date": old_date}

    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json=updated_company)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",
                "gst_registered": "true",
                "gst_effective_date": old_date,
                "backdate_confirmed": "true",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/settings/company?company_id={_COMPANY_ID}"


@pytest.mark.anyio
@respx.mock
async def test_company_settings_gst_recent_date_no_confirm(respx_mock: respx.MockRouter) -> None:
    """POST with a gst_effective_date within 21 days skips confirmation and saves directly."""
    from datetime import date, timedelta

    recent_date = (date.today() - timedelta(days=5)).isoformat()
    updated_company = {**_MOCK_COMPANY, "gst_registered": True, "gst_effective_date": recent_date}

    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json=updated_company)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",
                "gst_registered": "true",
                "gst_effective_date": recent_date,
            },
        )

    assert resp.status_code == 303


@pytest.mark.anyio
@respx.mock
async def test_company_settings_gst_old_date_no_invoices_skips_confirm(
    respx_mock: respx.MockRouter,
) -> None:
    """Recording a long-standing historical GST date with no pre-reg invoices saves silently.

    Common scenario: a business that has been GST-registered with the ATO
    for years is entering its real registration date into a freshly
    bootstrapped books instance. There are no pre-registration invoices
    to credit-note, so the system must NOT block on the backdate-confirm
    page — it should just save.
    """
    from datetime import date, timedelta

    old_date = (date.today() - timedelta(days=365 * 5)).isoformat()
    updated_company = {**_MOCK_COMPANY, "gst_registered": True, "gst_effective_date": old_date}

    respx_mock.get(
        f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}/gst-backdate-preview"
    ).mock(
        return_value=Response(200, json={"invoice_count": 0, "effective_date": old_date})
    )
    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json=updated_company)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",
                "gst_registered": "true",
                "gst_effective_date": old_date,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/settings/company?company_id={_COMPANY_ID}"


# ---------------------------------------------------------------------------
# PSI — PSI status field in company settings form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_company_settings_psi_field_renders(respx_mock: respx.MockRouter) -> None:
    """GET /settings/company renders the PSI status radio group."""
    mock_company_with_psi = {**_MOCK_COMPANY, "psi_status": "unsure"}
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [mock_company_with_psi], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/settings/company")

    assert resp.status_code == 200
    assert 'name="psi_status"' in resp.text
    assert "Personal Services Income" in resp.text
    assert "80/20" in resp.text
    # All three radio values present
    assert 'value="yes"' in resp.text
    assert 'value="no"' in resp.text
    assert 'value="unsure"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_company_settings_psi_update_sends_to_api(respx_mock: respx.MockRouter) -> None:
    """POST /settings/company with psi_status=yes sends it in the PATCH body."""
    captured: list[dict] = []

    def _capture(request: respx.Request, *_: object) -> Response:
        import json as _json
        captured.append(_json.loads(request.content))
        return Response(200, json={**_MOCK_COMPANY, "psi_status": "yes", "version": 4})

    respx_mock.patch(f"{_API_BASE}/api/v1/companies/{_COMPANY_ID}").mock(
        side_effect=_capture
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/settings/company",
            data={
                "company_id": _COMPANY_ID,
                "name": "Acme Pty Ltd",
                "version": "3",
                "psi_status": "yes",
            },
        )

    assert resp.status_code == 303
    assert captured, "PATCH was not called"
    assert captured[0].get("psi_status") == "yes"
