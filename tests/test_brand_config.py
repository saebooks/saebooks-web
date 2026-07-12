"""Tests for the deployment-level brand config — EE GUI prep Packet 1.

SAEBOOKS_BRAND selects the active brand (saebooks default | tasur) and is
injected as the `current_brand()` Jinja global via the existing security
patch (saebooks_web.security._patch_jinja_templates → brand.register_brand_global).

Six tests:
1. test_login_default_brand_is_saebooks   — unauthenticated /login, no env var:
                                             title/heading/meta exactly match
                                             today's hardcoded SAE Books text.
2. test_login_brand_tasur                 — SAEBOOKS_BRAND=tasur → /login shows
                                             Tasur in title/heading/copy, no
                                             leftover "SAE Books" in the hooked
                                             elements.
3. test_signup_brand_tasur                — same for /signup.
4. test_base_head_meta_default_vs_tasur   — application-name / apple-mobile-
                                             web-app-title / description /
                                             favicon hrefs swap with the brand.
5. test_sidebar_wordmark_and_footer_tasur — authenticated page: sidebar
                                             wordmark img src/alt + cmdk
                                             footer text swap with the brand.
6. test_unknown_brand_falls_back_to_saebooks — a typo'd SAEBOOKS_BRAND value
                                             degrades to the default brand
                                             instead of erroring.
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

_MOCK_ACCOUNTS_RESPONSE = {"items": [], "total": 0, "limit": 200, "offset": 0}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


# ---------------------------------------------------------------------------
# Unauthenticated pages — /login, /signup (full <head> + auth content block,
# no sidebar since that's gated on request.session api_token).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_default_brand_is_saebooks() -> None:
    """No SAEBOOKS_BRAND set → /login renders exactly today's SAE Books text."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert resp.status_code == 200
    assert "<title>Sign in — SAE Books</title>" in resp.text
    assert "Enter your SAE Books email address and password." in resp.text
    assert "New to SAE Books?" in resp.text
    assert 'content="SAE Books"' in resp.text  # application-name / apple-mobile-web-app-title
    assert "Tasur" not in resp.text


@pytest.mark.anyio
async def test_login_brand_tasur(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert resp.status_code == 200
    assert "<title>Sign in — Tasur</title>" in resp.text
    assert "Enter your Tasur email address and password." in resp.text
    assert "New to Tasur?" in resp.text
    assert 'content="Tasur"' in resp.text
    # The hooked elements must not still say SAE Books once branded.
    assert "SAE Books" not in resp.text


@pytest.mark.anyio
async def test_signup_brand_tasur(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/signup")

    assert resp.status_code == 200
    assert "<title>Sign up — Tasur</title>" in resp.text
    assert "Create your Tasur account</h1>" in resp.text
    assert "SAE Books" not in resp.text


@pytest.mark.anyio
async def test_base_head_meta_default_vs_tasur(monkeypatch: pytest.MonkeyPatch) -> None:
    """<head> meta/favicon hooks swap with the brand; default matches today."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        default_resp = await client.get("/login")

    assert '<meta name="description" content="API-first accounting for Australian small business.">' in default_resp.text
    assert '<link rel="icon" type="image/png" sizes="32x32" href="/static/pwa/icons/favicon-32.png">' in default_resp.text
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/pwa/icons/apple-touch-icon-180.png">' in default_resp.text

    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tasur_resp = await client.get("/login")

    assert '<meta name="description" content="API-first accounting for Estonian small business.">' in tasur_resp.text
    assert '<link rel="icon" type="image/png" sizes="32x32" href="/static/brand/tasur-favicon.png">' in tasur_resp.text
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/brand/tasur-apple-touch-icon.png">' in tasur_resp.text


# ---------------------------------------------------------------------------
# Authenticated page — sidebar wordmark + command-palette footer are gated
# on request.session['api_token'] (base.html line ~418), so hit a simple
# authenticated route (mirrors tests/test_accounts.py's session-cookie
# pattern) to exercise them.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_sidebar_wordmark_and_footer_tasur(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        default_resp = await client.get("/accounts")

    assert default_resp.status_code == 200
    assert '<img src="/static/sae-books-logo.png"\n           alt="SAE Books"' in default_resp.text
    assert ">SAE Books · ⌘K<" in default_resp.text

    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        tasur_resp = await client.get("/accounts")

    assert tasur_resp.status_code == 200
    assert '<img src="/static/brand/tasur-wordmark.png"\n           alt="Tasur"' in tasur_resp.text
    assert ">Tasur · ⌘K<" in tasur_resp.text


@pytest.mark.anyio
async def test_unknown_brand_falls_back_to_saebooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "not-a-real-brand")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert resp.status_code == 200
    assert "<title>Sign in — SAE Books</title>" in resp.text
