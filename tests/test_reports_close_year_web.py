"""Web tests for the year-end close page (ADMIN-gated).

GET previews the zeroing entry; POST posts the close + locks. Both gated to
admin / SAE-staff sessions. Backed by /api/v1/period-close/{preview,close-year}.
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

_API_BASE = settings.api_url.rstrip("/")
_EQ_ID = "33333333-3333-3333-3333-333333333333"
_JE_ID = "99999999-9999-9999-9999-999999999999"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode())).decode()


_ADMIN = _cookie({"api_token": "t", "user_role": "admin"})
_CLIENT = _cookie({"api_token": "t", "user_role": "client"})

_ACCOUNTS = {"items": [{"id": _EQ_ID, "code": "3-8000", "name": "Retained Earnings", "account_type": "EQUITY"}]}
_PREVIEW = {
    "through_date": "2025-06-30",
    "total_income": 100.0,
    "total_expenses": 40.0,
    "net_profit": 60.0,
    "has_anything_to_close": True,
    "retained_earnings_debit": 0.0,
    "retained_earnings_credit": 60.0,
    "lines": [{"account_id": _EQ_ID, "description": "Close to RE", "debit": 0.0, "credit": 60.0}],
}


@pytest.mark.anyio
@respx.mock
async def test_close_year_admin_get_200(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/accounts.*$").mock(return_value=Response(200, json=_ACCOUNTS))
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/period-close/preview.*$").mock(return_value=Response(200, json=_PREVIEW))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _ADMIN}) as client:
        r = await client.get("/reports/close-year")
    assert r.status_code == 200, r.text
    assert "Year-End Close" in r.text
    assert "Retained Earnings" in r.text
    assert "Post close-year journal" in r.text


@pytest.mark.anyio
@respx.mock
async def test_close_year_non_admin_403(respx_mock: respx.MockRouter) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _CLIENT}) as client:
        r = await client.get("/reports/close-year")
    assert r.status_code == 403, r.text


@pytest.mark.anyio
@respx.mock
async def test_close_year_post_redirects_to_entry(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API_BASE}/api/v1/period-close/close-year").mock(
        return_value=Response(200, json={"closed": True, "journal_entry_id": _JE_ID})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           cookies={settings.session_cookie_name: _ADMIN},
                           follow_redirects=False) as client:
        r = await client.post("/reports/close-year",
                              data={"through": "2025-06-30", "retained_earnings_account_id": _EQ_ID})
    assert r.status_code == 303
    assert r.headers["location"] == f"/journal-entries/{_JE_ID}"
    sent = _json.loads(route.calls.last.request.content)
    assert sent["through_date"] == "2025-06-30"
    assert sent["retained_earnings_account_id"] == _EQ_ID
