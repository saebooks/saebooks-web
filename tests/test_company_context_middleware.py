"""CompanyContextMiddleware typed failure surface (M2 app-lane step 8a).

Engine down → the page still renders (200) with the app-wide amber banner;
engine healthy → no banner.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-ctx", "locale": "en"}
)


@pytest.mark.anyio
@respx.mock
async def test_companies_down_shows_banner_not_500(
    respx_mock: respx.MockRouter,
) -> None:
    # The middleware's own /api/v1/companies fetch fails at connection level.
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        side_effect=httpx.ConnectError("engine down")
    )
    # The accounts page's own data call succeeds — page must still render.
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert "data-company-context-banner" in resp.text
    # Page content still rendered normally around the banner.
    assert "data-degraded-panel" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_companies_healthy_no_banner(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert "data-company-context-banner" not in resp.text
