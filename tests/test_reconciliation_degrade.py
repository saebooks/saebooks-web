"""Reconciliation degrade + pagination (M2 enterprise views).

Bar: engine unreachable → GET pages render their shell with the shared
degraded panel (never a 500); POST actions flash an engine-unreachable
message and redirect back (no misleading "failed" error). The lines page
paginates web-side at 50 per page.
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
_ACCOUNT_ID = "aaaaaaaa-1111-2222-3333-444444444444"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-recon", "locale": "en"}
)


def _quiet_side_fetches(respx_mock: respx.MockRouter) -> None:
    empty = {"items": []}
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=empty)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json=empty)
    )


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_index_engine_down_degrades(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        side_effect=httpx.ConnectError("engine down")
    )

    async with _client() as client:
        resp = await client.get("/reconciliation")

    assert resp.status_code == 200
    assert "data-degraded-panel" in resp.text
    assert "Bank Reconciliation" in resp.text  # shell survived


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_lines_engine_down_degrades(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        side_effect=httpx.ConnectError("engine down")
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        side_effect=httpx.ConnectError("engine down")
    )

    async with _client() as client:
        resp = await client.get(f"/reconciliation/{_ACCOUNT_ID}/lines")

    assert resp.status_code == 200
    assert "data-degraded-panel" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_match_engine_down_flashes_and_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.post(f"{_API_BASE}/api/v1/reconciliation/match").mock(
        side_effect=httpx.ConnectError("engine down")
    )

    async with _client() as client:
        resp = await client.post(
            "/reconciliation/match",
            data={
                "bsl_id": "b1",
                "entry_id": "e1",
                "account_id": _ACCOUNT_ID,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/reconciliation/{_ACCOUNT_ID}/lines"
    # Follow the redirect and check the engine-unreachable flash (not a
    # misleading "Match failed").
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[])
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        return_value=Response(200, json=[])
    )
    async with _client() as client:
        client.cookies.update(resp.cookies)
        follow = await client.get(resp.headers["location"])
    assert "could not be reached" in follow.text
    assert "nothing was changed" in follow.text


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_lines_paginate_at_50(
    respx_mock: respx.MockRouter,
) -> None:
    _quiet_side_fetches(respx_mock)
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(
            200, json=[{"id": _ACCOUNT_ID, "name": "Cheque", "code": "1-1110"}]
        )
    )
    lines = [
        {
            "id": f"bsl-{i}",
            "txn_date": "2026-01-01",
            "description": f"LINE-{i:03d}",
            "amount": "-1.00",
        }
        for i in range(120)
    ]
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        return_value=Response(200, json=lines)
    )

    async with _client() as client:
        page1 = await client.get(f"/reconciliation/{_ACCOUNT_ID}/lines")
        page3 = await client.get(
            f"/reconciliation/{_ACCOUNT_ID}/lines?offset=100&limit=50"
        )

    assert page1.status_code == 200
    assert "LINE-000" in page1.text
    assert "LINE-049" in page1.text
    assert "LINE-050" not in page1.text
    # Pagination controls present with the full total.
    assert "120" in page1.text

    assert page3.status_code == 200
    assert "LINE-100" in page3.text
    assert "LINE-119" in page3.text
    assert "LINE-099" not in page3.text
