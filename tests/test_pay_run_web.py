"""``/pay-run`` is retired (gap P1-60, batch 9).

It was the old creditor ABA screen, listed in the menu as "Pay Run" next to
the real wages pay runs. Every old address now redirects to
``/payroll/pay-runs`` and the menu no longer offers it.
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
from tests import _jp

_API_BASE = settings.api_url.rstrip("/")
_RUN = "cccccccc-1111-0000-0000-000000000001"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "test-token-payrun"}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _client(authed: bool = True) -> AsyncClient:
    cookies = {settings.session_cookie_name: _cookie()} if authed else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       cookies=cookies, follow_redirects=False)


@pytest.mark.anyio
async def test_pay_run_requires_auth() -> None:
    async with _client(authed=False) as c:
        r = await c.get("/pay-run")
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.anyio
@pytest.mark.parametrize(("old", "new"), [
    ("/pay-run", "/payroll/pay-runs"),
    ("/pay-run/new", "/payroll/pay-runs"),
    (f"/pay-run/{_RUN}", f"/payroll/pay-runs/{_RUN}"),
])
async def test_old_addresses_redirect(old: str, new: str) -> None:
    async with _client() as c:
        r = await c.get(old)
    assert r.status_code == 303 and r.headers["location"] == new


@pytest.mark.anyio
async def test_old_posts_redirect_without_calling_the_engine() -> None:
    with respx.mock(assert_all_called=False) as rm:
        export = rm.post(url__regex=r".*/api/v1/pay-runs/.*/export-aba").mock(
            return_value=Response(200, json={}))
        async with _client() as c:
            r = await c.post(f"/pay-run/{_RUN}/export-aba", data={})
    assert r.status_code == 303 and r.headers["location"] == "/payroll/pay-runs"
    assert not export.called


@pytest.mark.anyio
@respx.mock
async def test_menu_has_one_pay_run_entry(respx_mock: respx.MockRouter) -> None:
    _jp.mock_au_context(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/payments").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/contacts.*$").mock(
        return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        r = await c.get("/payments")
    assert r.status_code == 200
    html = r.text
    assert 'href="/pay-run"' not in html and 'href="/pay-run/new"' not in html
    assert 'href="/payroll/pay-runs"' in html
    assert 'href="/payroll/stp"' in html and 'href="/payroll/super"' in html
    assert 'href="/payroll/year-end"' in html
    assert "coming soon', href: '/pay-run'" not in html
