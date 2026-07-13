"""Tests for the ephemeral-demo isolation surface (Packet A1, §6b).

Covers the passive badge (base.html partial) and the live RLS probe route
(``/demo/isolation``). Upstream HTTP is mocked with ``respx`` and the tests
drive the REAL app so base.html renders through the full middleware stack
(CompanyContextMiddleware populates ``request.state.active_company_name`` for
the badge) — the same "real app + respx" approach test 11 of
``test_demo_autologin.py`` uses.

Matrix
------
1. Demo session → GET /demo/isolation renders the badge WITH the tenant
   suffix (…last-6) and the isolation card.
2. Non-demo session (ephemeral OFF, no demo marker) → 404 on the route,
   and the badge does not render.
3. Malformed uuid → 400, and the contacts endpoint is NEVER called.
4. Well-formed uuid → contacts GET is proxied; a 404 upstream renders the
   "row does not exist for you … row-level security" outcome.
"""
from __future__ import annotations

import json
import os
from base64 import b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner

from saebooks_web.config import settings

_API_BASE = settings.api_url.rstrip("/")
_AUTH_ME_URL = f"{_API_BASE}/api/v1/auth/me"
_COMPANIES_URL = f"{_API_BASE}/api/v1/companies"
_TAX_CODES_URL = f"{_API_BASE}/api/v1/tax_codes"

_TENANT_ID = "11111111-2222-3333-4444-abcdef123456"   # suffix …123456
_COMPANY_ID = "99999999-8888-7777-6666-555544443333"
_FOREIGN_UUID = "00000000-0000-4000-8000-000000000000"

_EPHEMERAL_ENV = {
    "SAEBOOKS_DEMO_EPHEMERAL": "1",
    "DEMO_INTERNAL_SECRET": "test-secret",
    "SAEBOOKS_DEMO_LAND_PATH": "/",
    "SAEBOOKS_DEMO_TURNSTILE_ENABLED": "0",
}
_NON_DEMO_ENV = {
    "SAEBOOKS_DEMO_EPHEMERAL": "0",
    "SAEBOOKS_DEMO_TURNSTILE_ENABLED": "0",
}


def _make_session_cookie(data: dict) -> str:
    signer = TimestampSigner(settings.secret_key)
    payload = b64encode(json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _demo_session() -> dict:
    return {
        "api_token": "demo-token-live",
        "demo_tenant_id": _TENANT_ID,
        "demo_company_id": _COMPANY_ID,
        "demo_provisioned_at": "07:42",
        # Pin the render locale to English so assertions match the source
        # strings. In production the badge/card render in the visitor's
        # locale (EE jurisdiction → Estonian); the LocaleMiddleware honours
        # this session override above the jurisdiction default.
        "locale": "en",
    }


def _client(cookie: str | None = None) -> AsyncClient:
    from saebooks_web.main import app as main_app  # noqa: PLC0415

    cookies = {}
    if cookie:
        cookies[settings.session_cookie_name] = cookie
    return AsyncClient(
        transport=ASGITransport(main_app),
        base_url="http://test",
        cookies=cookies,
    )


def _stub_company_context(respx_mock: respx.MockRouter) -> None:
    """Stub the calls CompanyContextMiddleware + the demo middleware make so
    base.html can render (auth/me for binding-key reuse, companies + tax_codes
    for request.state)."""
    respx_mock.get(_AUTH_ME_URL).mock(
        return_value=Response(200, json={"email": "demo@example.com", "role": "admin"})
    )
    respx_mock.get(_COMPANIES_URL).mock(
        return_value=Response(
            200,
            json={"items": [{"id": _COMPANY_ID, "name": "Näidis OÜ (demo)", "created_at": "2026-07-13T00:00:00Z"}]},
        )
    )
    respx_mock.get(_TAX_CODES_URL).mock(
        return_value=Response(200, json={"items": [{"jurisdiction": "EE"}]})
    )


# ---------------------------------------------------------------------------
# 1. Demo session → badge with tenant suffix + isolation card
# ---------------------------------------------------------------------------
@pytest.mark.anyio
@respx.mock
async def test_banner_and_card_render_in_demo_mode(respx_mock: respx.MockRouter) -> None:
    _stub_company_context(respx_mock)

    with patch_env(_EPHEMERAL_ENV):
        async with _client(cookie=_make_session_cookie(_demo_session())) as client:
            resp = await client.get("/demo/isolation", follow_redirects=False)

    assert resp.status_code == 200
    # Passive badge present, with the last-6 tenant suffix and the marketing copy.
    assert "Private sandbox" in resp.text
    assert "…123456" in resp.text
    # Company name resolved via CompanyContextMiddleware.
    assert "Näidis OÜ (demo)" in resp.text
    # Isolation card + policy link.
    assert "Read the isolation policy source" in resp.text
    assert "github.com/saebooks/saebooks" in resp.text


# ---------------------------------------------------------------------------
# 2. Non-demo session → 404, no badge
# ---------------------------------------------------------------------------
@pytest.mark.anyio
@respx.mock
async def test_non_demo_session_404(respx_mock: respx.MockRouter) -> None:
    # CompanyContext may still fire; stub companies so no real network is hit.
    respx_mock.get(_COMPANIES_URL).mock(return_value=Response(200, json={"items": []}))
    respx_mock.get(_TAX_CODES_URL).mock(return_value=Response(200, json={"items": []}))

    with patch_env(_NON_DEMO_ENV):
        # A plain authenticated session with NO demo marker.
        cookie = _make_session_cookie({"api_token": "plain-token"})
        async with _client(cookie=cookie) as client:
            resp = await client.get("/demo/isolation", follow_redirects=False)

    assert resp.status_code == 404
    assert "Private sandbox" not in resp.text


# ---------------------------------------------------------------------------
# 3. Malformed uuid → 400, engine (contacts) never called
# ---------------------------------------------------------------------------
@pytest.mark.anyio
@respx.mock
async def test_malformed_uuid_400_no_engine_call(respx_mock: respx.MockRouter) -> None:
    _stub_company_context(respx_mock)
    contacts_route = respx_mock.get(url__regex=rf"{_API_BASE}/api/v1/contacts/.*").mock(
        return_value=Response(404)
    )

    with patch_env(_EPHEMERAL_ENV):
        async with _client(cookie=_make_session_cookie(_demo_session())) as client:
            resp = await client.get("/demo/isolation?probe=not-a-uuid", follow_redirects=False)

    assert resp.status_code == 400
    assert contacts_route.call_count == 0


# ---------------------------------------------------------------------------
# 4. Well-formed uuid → contacts proxied, 404 outcome rendered
# ---------------------------------------------------------------------------
@pytest.mark.anyio
@respx.mock
async def test_wellformed_uuid_proxies_and_renders_404(respx_mock: respx.MockRouter) -> None:
    _stub_company_context(respx_mock)
    contacts_route = respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_FOREIGN_UUID}").mock(
        return_value=Response(404, json={"detail": "not found"})
    )

    with patch_env(_EPHEMERAL_ENV):
        async with _client(cookie=_make_session_cookie(_demo_session())) as client:
            resp = await client.get(f"/demo/isolation?probe={_FOREIGN_UUID}", follow_redirects=False)

    assert resp.status_code == 200
    assert contacts_route.call_count == 1
    assert "row-level security" in resp.text
    assert "does not exist for you" in resp.text


# ---------------------------------------------------------------------------
# 5. Banner partial: gated purely on the demo_tenant_id session marker —
#    renders with the tenant suffix for a demo session, nothing otherwise.
# ---------------------------------------------------------------------------
def test_banner_partial_gated_on_demo_marker() -> None:
    import saebooks_web.main  # noqa: PLC0415  — registers Jinja globals
    from saebooks_web.routes.demo_isolation import _TEMPLATES  # noqa: PLC0415

    tmpl = _TEMPLATES.env.get_template("_partials/demo_banner.html")

    class _State:
        active_company_name = "Näidis OÜ"

    class _Req:
        state = _State()
        scope = {"session": {}}
        url = type("U", (), {"path": "/dashboard"})()

        def __init__(self, session):
            self.session = session

    demo = _Req({"demo_tenant_id": _TENANT_ID, "demo_provisioned_at": "07:42"})
    plain = _Req({})

    demo_html = tmpl.render(request=demo)
    plain_html = tmpl.render(request=plain)

    assert "Private sandbox" in demo_html
    assert "…123456" in demo_html          # last-6 tenant suffix
    assert plain_html.strip() == ""          # absent for a non-demo session


# ---------------------------------------------------------------------------
# helper: env patch context manager
# ---------------------------------------------------------------------------
from contextlib import contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402


@contextmanager
def patch_env(env: dict):
    with patch.dict(os.environ, env, clear=False):
        yield
