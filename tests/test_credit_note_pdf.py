"""Tests for GET /credit-notes/{id}/pdf — web proxy.

Three tests:
1. test_credit_note_pdf_proxy_streams_pdf — 200; content-type forwarded; bytes match.
2. test_credit_note_pdf_proxy_auth_required — no session → 303 /login.
3. test_credit_note_pdf_proxy_404 — upstream 404 → 404.
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

_CN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_FAKE_PDF = b"%PDF-1.5 fake-cn-pdf-web"
_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-xyz"})


@pytest.mark.anyio
async def test_credit_note_pdf_proxy_auth_required() -> None:
    """GET /credit-notes/{id}/pdf without session → 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}/pdf")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_credit_note_pdf_proxy_streams_pdf(respx_mock: respx.MockRouter) -> None:
    """GET /credit-notes/{id}/pdf → 200 application/pdf bytes forwarded from API."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/pdf").mock(
        return_value=Response(
            200,
            content=_FAKE_PDF,
            headers={
                "content-type": "application/pdf",
                "content-disposition": f'inline; filename="credit-note-CN-001.pdf"',
            },
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF
    assert "credit-note" in resp.headers.get("content-disposition", "")


@pytest.mark.anyio
@respx.mock
async def test_credit_note_pdf_proxy_404(respx_mock: respx.MockRouter) -> None:
    """Upstream 404 → web layer raises 404."""
    respx_mock.get(f"{_API_BASE}/api/v1/credit_notes/{_CN_ID}/pdf").mock(
        return_value=Response(404, json={"detail": "Credit note not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/credit-notes/{_CN_ID}/pdf")

    assert resp.status_code == 404, resp.text
