"""cashbook_invoices guard degrade (M2 app-lane step 10).

Engine down and wrong-bookkeeping-mode must produce visibly different
outcomes: 503 degraded panel vs the existing "Cashbook companies only"
flash-redirect (which must keep passing unchanged).
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
    {"api_token": "test-token-cb", "locale": "en"}
)


@pytest.mark.anyio
@respx.mock
async def test_engine_down_shows_degraded_panel_not_wrong_mode(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        side_effect=httpx.ConnectError("engine down")
    )
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/cashbook/invoices")

    # Engine-down is a 503 degraded panel — NOT the misleading
    # "Cashbook companies only" redirect to /.
    assert resp.status_code == 503
    assert "data-degraded-panel" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_wrong_mode_still_flash_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "name": "Standard Co",
                        "bookkeeping_mode": "standard",
                    }
                ]
            },
        )
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
        follow_redirects=False,
    ) as client:
        resp = await client.get("/cashbook/invoices")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
