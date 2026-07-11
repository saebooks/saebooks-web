"""Tests for jurisdiction-aware UI surface — Packet 1.

CompanyOut does not expose ``Company.jurisdiction`` (verified against the
engine's saebooks/api/v1/schemas.py on feat/m1-m15-global), so
CompanyContextMiddleware resolves the active company's jurisdiction by
proxying off the default (no explicit ``jurisdiction`` param) response of
``GET /api/v1/tax_codes`` — the engine's Packet 4a change makes that list
default to the requesting company's own jurisdiction. See
saebooks_web/company_context.py module docstring for the full rationale.

Six tests:
1. test_jurisdiction_defaults_to_au_when_tax_codes_empty   — no tax codes at
   all yet -> jurisdiction resolves to "AU" (matches the engine's own
   fallback), same as an unresolved/logged-out request.
2. test_jurisdiction_resolves_from_tax_codes_response       — tax_codes item
   carries jurisdiction="EE" -> request.state.active_company_jurisdiction
   picks it up.
3. test_dashboard_au_regression_gst_widgets_present          — AU company:
   GST hero chip, KPI tile, gauge and compliance banner still render
   (byte-identical to pre-Packet-1 behaviour).
4. test_dashboard_ee_hides_gst_widgets                        — EE company:
   none of the AU GST-registration-threshold widgets render.
5. test_nav_au_shows_bas_and_ato_sbr_links                    — AU: sidebar
   "GST" section label, BAS worksheet link, and (admin) ATO SBR link present.
6. test_nav_ee_hides_bas_and_ato_sbr_shows_tax_codes          — EE: sidebar
   section relabelled "Tax", BAS worksheet + ATO SBR links absent, Tax Codes
   link still present.
7. test_invoice_new_tax_code_dropdown_sends_no_jurisdiction_param — regression
   guard on the actual dropdown surface (invoice line tax-code select): the
   fetch never sends an explicit ``jurisdiction`` query param, so it always
   gets the engine's own per-company default.
8. test_invoice_new_ee_dropdown_shows_engine_tax_code_name         — an EE
   company's invoice-new form renders the tax code's engine-provided
   ``name``/``tax_system`` verbatim (VAT), not a hand-localised label.
9. test_jurisdiction_lookup_uses_limit_not_page_size               — the
   middleware's own tax_codes probe call uses tax_codes' actual
   ``limit``/``offset`` params, not companies' ``page``/``page_size`` (which
   FastAPI would silently ignore, falling back to the 200-row default).
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

from tests.test_dashboard import _register_mocks, _ytd_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-jurisdiction"})
_API_BASE = settings.api_url.rstrip("/")

_AU_COMPANY_ID = "a0000000-0000-0000-0000-00000000000a"
_EE_COMPANY_ID = "e0000000-0000-0000-0000-00000000000e"

# CompanyOut never exposes ``jurisdiction`` — this is deliberately absent
# from both fixtures, matching the real engine response shape.
_AU_COMPANY = {
    "id": _AU_COMPANY_ID,
    "name": "Acme Pty Ltd",
    "trading_name": "Acme",
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}
_EE_COMPANY = {
    "id": _EE_COMPANY_ID,
    "name": "Acme OU",
    "trading_name": "Acme OU",
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}


def _mock_companies(respx_mock: respx.MockRouter, company: dict) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )


def _mock_tax_codes(respx_mock: respx.MockRouter, jurisdiction: str | None) -> None:
    """Mock the default (no explicit jurisdiction param) tax_codes response
    the way the engine now returns it — items pre-filtered/stamped with the
    resolved company jurisdiction. ``jurisdiction=None`` simulates a brand
    new company with no tax codes at all yet.
    """
    items = []
    if jurisdiction is not None:
        items = [
            {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "code": "T1",
                "name": "Test code",
                "rate": "10.000",
                "tax_system": "GST" if jurisdiction == "AU" else "VAT",
                "jurisdiction": jurisdiction,
            }
        ]
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_codes(\?.*)?$").mock(
        return_value=Response(200, json={"items": items, "total": len(items)})
    )


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


# ---------------------------------------------------------------------------
# 1-2. CompanyContextMiddleware jurisdiction resolution
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_jurisdiction_defaults_to_au_when_tax_codes_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """A company with zero tax codes yet resolves to 'AU', matching the
    engine's own fallback (Company.jurisdiction defaults to 'AU')."""
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction=None)
    _register_mocks(respx_mock)

    async with _client() as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # AU-only nav affordance still present -> jurisdiction resolved "AU".
    assert "BAS worksheet" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_jurisdiction_resolves_from_tax_codes_response(
    respx_mock: respx.MockRouter,
) -> None:
    """An EE company's tax_codes response (jurisdiction='EE') drives the
    resolved jurisdiction through to the rendered nav."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="EE")
    _register_mocks(respx_mock)

    async with _client() as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "BAS worksheet" not in resp.text


# ---------------------------------------------------------------------------
# 3-4. Dashboard GST widgets — AU regression + EE hide
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_dashboard_au_regression_gst_widgets_present(
    respx_mock: respx.MockRouter,
) -> None:
    """AU company: dashboard renders the same GST widgets as before Packet 1."""
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="AU")
    _register_mocks(
        respx_mock,
        ytd_data=_ytd_response(ytd_turnover=45000.0, threshold_crossed=False),
    )

    async with _client() as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "GST turnover" in resp.text
    assert "GST · under threshold" in resp.text
    assert "of $75,000 threshold" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_dashboard_ee_hides_gst_widgets(respx_mock: respx.MockRouter) -> None:
    """EE company: none of the AU GST-registration-threshold widgets render."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="EE")
    _register_mocks(
        respx_mock,
        ytd_data=_ytd_response(ytd_turnover=78000.0, threshold_crossed=True),
    )

    async with _client() as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "GST turnover" not in resp.text
    assert "GST · under threshold" not in resp.text
    assert "GST · register within 21 days" not in resp.text
    assert "of $75,000 threshold" not in resp.text
    assert "you must register with the ATO" not in resp.text


# ---------------------------------------------------------------------------
# 5-6. Sidebar nav — AU vs EE
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_nav_au_shows_bas_and_ato_sbr_links(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="AU")
    _register_mocks(respx_mock)

    session = _make_session_cookie(
        {"api_token": "test-token-jurisdiction", "user_role": "admin"}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: session},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert ">GST<" in resp.text
    assert "BAS worksheet" in resp.text
    assert "ATO SBR" in resp.text
    assert "Tax Codes" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_nav_ee_hides_bas_and_ato_sbr_shows_tax_codes(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="EE")
    _register_mocks(respx_mock)

    session = _make_session_cookie(
        {"api_token": "test-token-jurisdiction", "user_role": "admin"}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: session},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert ">GST<" not in resp.text
    assert ">Tax<" in resp.text
    assert "BAS worksheet" not in resp.text
    assert "ATO SBR" not in resp.text
    # Jurisdiction-neutral — the engine's own tax-code list stays reachable.
    assert "Tax Codes" in resp.text


# ---------------------------------------------------------------------------
# 7-8. Invoice-new tax-code dropdown — no jurisdiction override, EE names
#      come through verbatim from the engine
# ---------------------------------------------------------------------------


def _mock_invoice_new_dropdowns(
    respx_mock: respx.MockRouter, *, tax_codes_mock
) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/contacts(\?.*)?$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/accounts(\?.*)?$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/projects(\?.*)?$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_codes(\?.*)?$").mock(
        side_effect=tax_codes_mock
    )


@pytest.mark.anyio
@respx.mock
async def test_invoice_new_tax_code_dropdown_sends_no_jurisdiction_param(
    respx_mock: respx.MockRouter,
) -> None:
    """Regression guard on the actual dropdown surface: the invoice-line
    tax-code fetch must not send jurisdiction=AU (or any other explicit
    override), so a non-AU company's own default (engine Packet 4a) is
    what actually comes back."""
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        return Response(200, json={"items": [], "total": 0})

    _mock_invoice_new_dropdowns(respx_mock, tax_codes_mock=_capture)

    async with _client() as client:
        resp = await client.get("/invoices/new")

    assert resp.status_code == 200
    assert "params" in captured, "tax_codes endpoint was never called"
    assert "jurisdiction" not in captured["params"]


@pytest.mark.anyio
@respx.mock
async def test_invoice_new_ee_dropdown_shows_engine_tax_code_name(
    respx_mock: respx.MockRouter,
) -> None:
    """An EE company's invoice-new form renders the engine-provided tax
    code name/tax_system verbatim (e.g. VAT) rather than a hand-localised
    label — the app must not invent "käibemaks" or similar itself."""

    def _ee_tax_codes(request):
        return Response(
            200,
            json={
                "items": [
                    {
                        "id": "bbbbbbbb-0000-0000-0000-000000000002",
                        "code": "KM24",
                        "name": "Käibemaks 24%",
                        "rate": "24.000",
                        "tax_system": "VAT",
                        "jurisdiction": "EE",
                    }
                ],
                "total": 1,
            },
        )

    _mock_invoice_new_dropdowns(respx_mock, tax_codes_mock=_ee_tax_codes)

    async with _client() as client:
        resp = await client.get("/invoices/new")

    assert resp.status_code == 200
    # The engine's own name comes through untouched — the web app doesn't
    # hand-localise or substitute its own "VAT"/"GST" business-term copy.
    assert "Käibemaks 24%" in resp.text


# ---------------------------------------------------------------------------
# 9. Middleware jurisdiction probe uses tax_codes' real params
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_jurisdiction_lookup_uses_limit_not_page_size(
    respx_mock: respx.MockRouter,
) -> None:
    """CompanyContextMiddleware's own tax_codes probe (used to resolve
    jurisdiction, see module docstring) must use tax_codes' real
    limit/offset params, not companies' page/page_size — the latter would
    be silently dropped by FastAPI, falling back to a 200-row default
    fetch on every request instead of a cheap 1-row probe."""
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        return Response(200, json={"items": [], "total": 0})

    _mock_companies(respx_mock, _AU_COMPANY)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_codes(\?.*)?$").mock(
        side_effect=_capture
    )
    _register_mocks(respx_mock)

    async with _client() as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert captured["params"].get("limit") == "1"
    assert "page_size" not in captured["params"]
