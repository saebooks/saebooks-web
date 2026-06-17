"""Web proxy tests for GET /reports/statement-pack/pdf.

Tests:
* test_statement_pack_pdf_proxy_returns_pdf — respx mock → 200, application/pdf
* test_statement_pack_pdf_proxy_passes_params — params forwarded to API
* test_statement_pack_pdf_proxy_unauthenticated — no session → 303 to /login
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
_FAKE_PDF = b"%PDF-1.5 fake statement pack pdf"


@pytest.mark.asyncio
async def test_statement_pack_pdf_proxy_returns_pdf(
    respx_mock: respx.MockRouter,
) -> None:
    """Proxy returns PDF bytes with application/pdf content-type."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/statement_pack\.pdf.*$"
    ).mock(
        return_value=Response(
            200,
            content=_FAKE_PDF,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'inline; filename="statement-pack.pdf"',
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/statement-pack/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF
    assert "statement-pack.pdf" in resp.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_statement_pack_pdf_proxy_passes_query_params(
    respx_mock: respx.MockRouter,
) -> None:
    """Query params (as_of_date, from_date, to_date, comparative) are forwarded."""
    api_route = respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/statement_pack\.pdf.*$"
    ).mock(
        return_value=Response(
            200,
            content=_FAKE_PDF,
            headers={"content-type": "application/pdf"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get(
            "/reports/statement-pack/pdf",
            params={
                "as_of_date": "2025-06-30",
                "from_date": "2024-07-01",
                "to_date": "2025-06-30",
                "comparative": "true",
            },
        )

    assert resp.status_code == 200, resp.text
    assert len(api_route.calls) == 1
    called_url = str(api_route.calls[0].request.url)
    assert "as_of_date=2025-06-30" in called_url
    assert "from_date=2024-07-01" in called_url
    assert "to_date=2025-06-30" in called_url
    assert "comparative=true" in called_url


@pytest.mark.asyncio
async def test_statement_pack_pdf_proxy_unauthenticated() -> None:
    """No session cookie → redirect to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/reports/statement-pack/pdf")

    assert resp.status_code == 303
    assert resp.headers.get("location", "").endswith("/login")


@pytest.mark.asyncio
async def test_statement_pack_pdf_proxy_upstream_error(
    respx_mock: respx.MockRouter,
) -> None:
    """When API returns 502, proxy returns 502."""
    respx_mock.get(
        url__regex=rf"^{_API_BASE}/api/v1/reports/statement_pack\.pdf.*$"
    ).mock(
        return_value=Response(502, json={"detail": "LaTeX compile error: ..."})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/statement-pack/pdf")

    assert resp.status_code == 502
