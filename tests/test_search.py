"""Tests for the global search page — Lane D cycle 42.

Four tests:
1. test_search_page_loads      — GET /search returns 200 (no query)
2. test_search_with_query      — GET /search?q=test returns 200 with mocked results
3. test_search_empty_query     — GET /search?q=  returns 200 with "enter a search" prompt
4. test_search_htmx_partial    — HX-Request header returns partial fragment (no <html>)
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
# Helpers / fixtures
# ---------------------------------------------------------------------------

_CONTACT_ID = "abcdefab-abcd-abcd-abcd-abcdefabcdef"

_MOCK_SEARCH_RESPONSE = {
    "query": "test",
    "hits": [
        {
            "id": _CONTACT_ID,
            "kind": "contact",
            "title": "Test Corp",
            "subtitle": None,
            "url": f"/contacts/{_CONTACT_ID}",
        }
    ],
    "total": 1,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_page_loads() -> None:
    """GET /search (no query param) returns 200 with search form."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/search")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Search" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_search_with_query(respx_mock: respx.MockRouter) -> None:
    """GET /search?q=test returns 200 with mocked result title and kind badge."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/search\?.*q=test.*$").mock(
        return_value=Response(200, json=_MOCK_SEARCH_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/search", params={"q": "test"})

    assert resp.status_code == 200
    assert "Test Corp" in resp.text
    # Kind badge label
    assert "Contact" in resp.text
    # Result count
    assert "1 result" in resp.text
    # Linked to the correct web URL
    assert f"/contacts/{_CONTACT_ID}" in resp.text


@pytest.mark.anyio
async def test_search_empty_query() -> None:
    """GET /search?q= (empty string) returns 200 with the enter-a-search prompt."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/search", params={"q": ""})

    assert resp.status_code == 200
    assert "Enter a search term" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_search_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /search?q=foo with HX-Request header returns partial (no <html> wrapper)."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/search\?.*q=foo.*$").mock(
        return_value=Response(
            200,
            json={
                "query": "foo",
                "hits": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "kind": "invoice",
                        "title": "INV-000001",
                        "subtitle": "Foo Corp — $1,200.00",
                        "url": "/invoices/11111111-1111-1111-1111-111111111111",
                    }
                ],
                "total": 1,
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        headers={"HX-Request": "true"},
    ) as client:
        resp = await client.get("/search", params={"q": "foo"})

    assert resp.status_code == 200
    # Partial must NOT include the outer HTML shell.
    assert "<html" not in resp.text
    # But it must contain the hit content.
    assert "INV-000001" in resp.text
    assert "Foo Corp" in resp.text
    # Invoice kind badge.
    assert "Invoice" in resp.text
