"""Tests for GET /contacts/{id}/statement/pdf — web proxy.

Four tests:
1. auth_required — no session → 303 /login.
2. streams_pdf — 200; content-type + disposition forwarded; bytes match.
3. defaults_period — omitting from/to forwards a trailing-12-month window.
4. proxy_404 — upstream 404 → 404.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from datetime import date, timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_FAKE_PDF = b"%PDF-1.5 fake-contact-statement-web"
_API_BASE = settings.api_url.rstrip("/")
_UPSTREAM = f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}/statement.pdf"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-xyz"})


@pytest.mark.anyio
async def test_contact_statement_pdf_auth_required() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/statement/pdf")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_contact_statement_pdf_streams_pdf(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(_UPSTREAM).mock(
        return_value=Response(
            200,
            content=_FAKE_PDF,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'inline; filename="statement-Acme.pdf"',
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/statement/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF
    assert "statement" in resp.headers.get("content-disposition", "")


@pytest.mark.anyio
@respx.mock
async def test_contact_statement_pdf_defaults_period(respx_mock: respx.MockRouter) -> None:
    """Omitting from/to forwards a trailing-12-month window ending today."""
    route = respx_mock.get(_UPSTREAM).mock(
        return_value=Response(200, content=_FAKE_PDF,
                              headers={"content-type": "application/pdf"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/statement/pdf")

    assert resp.status_code == 200, resp.text
    sent = route.calls.last.request
    today = date.today()
    assert sent.url.params["to"] == today.isoformat()
    assert sent.url.params["from"] == (today - timedelta(days=365)).isoformat()


@pytest.mark.anyio
@respx.mock
async def test_contact_statement_pdf_404(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(_UPSTREAM).mock(
        return_value=Response(404, json={"detail": "Contact not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/statement/pdf")

    assert resp.status_code == 404, resp.text
