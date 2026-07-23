"""JurisdictionGateMiddleware — AU-only pages redirect a resolved EE company.

The nav hides AU payroll/BAS pages for EE; this proves a DIRECT URL to one also
can't render — the middleware redirects to /. An AU company passes through; an
unresolved jurisdiction is NOT blocked (nav-only cosmetics there).
"""
from __future__ import annotations

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks_web.config import settings
from saebooks_web.main import app
from tests import _jp
from tests.test_jurisdiction_gating import _make_session_cookie

_COOKIE = _make_session_cookie({"api_token": "gate-token", "user_role": "admin"})
_API = settings.api_url.rstrip("/")


def _mock_page_data(respx_mock):
    # generic empty lists so the AU page can render if it isn't gated
    respx_mock.get(url__regex=rf"^{_API}/api/v1/.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )


@pytest.mark.anyio
@respx.mock
async def test_ee_direct_url_to_employees_redirects(respx_mock: respx.MockRouter) -> None:
    _jp.mock_ee_context(respx_mock)
    _mock_page_data(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _COOKIE}) as c:
        resp = await c.get("/employees", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


@pytest.mark.anyio
@respx.mock
async def test_ee_direct_url_to_super_funds_redirects(respx_mock: respx.MockRouter) -> None:
    _jp.mock_ee_context(respx_mock)
    _mock_page_data(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _COOKIE}) as c:
        resp = await c.get("/super-funds", follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.anyio
@respx.mock
async def test_ee_direct_url_to_bas_report_redirects(respx_mock: respx.MockRouter) -> None:
    _jp.mock_ee_context(respx_mock)
    _mock_page_data(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _COOKIE}) as c:
        resp = await c.get("/reports/bas-summary", follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.anyio
@respx.mock
async def test_au_direct_url_to_employees_not_gated(respx_mock: respx.MockRouter) -> None:
    _jp.mock_au_context(respx_mock)
    _mock_page_data(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _COOKIE}) as c:
        resp = await c.get("/employees", follow_redirects=False)
    # AU has payroll → the gate does NOT redirect (200, or the page's own code)
    assert resp.status_code != 303
