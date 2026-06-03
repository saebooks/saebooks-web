"""Web smoke for the financial statement pack (saebooks-web rebuild).

GET /reports/statement-pack bundles P&L + Balance Sheet + Trial Balance into
one printable document with a cover page and trustee declaration, reusing the
existing /api/v1/reports/* endpoints and per-statement table fragments.
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


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")

_PL = {"income": {}, "expenses": {}, "net_profit": 0.0}
_BS: dict = {}
_TB: dict = {}
_COMPANIES = {
    "items": [
        {
            "name": "Sauer Pty Ltd",
            "legal_name": "Sauer Pty Ltd ATF Saueesti Trust",
            "acn": "683 275 756",
            "abn": "",
        }
    ]
}


@pytest.mark.asyncio
async def test_statement_pack_get_200(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/profit_loss.*$"
    ).mock(return_value=Response(200, json=_PL))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/balance_sheet.*$"
    ).mock(return_value=Response(200, json=_BS))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/trial_balance.*$"
    ).mock(return_value=Response(200, json=_TB))
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/companies.*$"
    ).mock(return_value=Response(200, json=_COMPANIES))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/statement-pack")

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Special Purpose Financial Statements" in body
    assert "Statement of Profit or Loss" in body
    assert "Statement of Financial Position" in body
    assert "Trial Balance" in body
    assert "Trustee" in body
    assert "Sauer Pty Ltd ATF Saueesti Trust" in body
