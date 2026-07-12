"""Tests for POST /set-locale — the language-switcher endpoint (Packet 2b).

Cases:
1. Anonymous POST sets the saebooks_locale cookie and redirects to `next`.
2. Logged-in POST persists the choice into the session (round-trip: a
   follow-up request using the returned session cookie renders in the new
   locale — proves the middleware actually reads what the switcher wrote,
   not just that the switcher claims success).
3. An unsupported locale value is a silent no-op redirect (no cookie/session
   write), not a 400/500.
4. `next` is same-origin-only — an absolute/cross-origin value falls back to "/".

Fixer round 4:
5. No `next` field but a same-origin Referer header -- redirects back to the
   referring page's path, not "/". (Real browsers always send Referer as an
   absolute URL, so the pre-fix same-origin check -- which rejected any
   value carrying a scheme/netloc -- rejected same-origin referers exactly
   like cross-origin ones and always fell back to "/".)
6. No `next` field, a cross-origin Referer header -- still falls back to "/".
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode, urlsafe_b64decode as _b64decode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

from tests.test_dashboard import _register_mocks

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _decode_session_cookie(raw: str) -> dict:
    """Inverse of _make_session_cookie — reads back what the app set."""
    signer = _TimestampSigner(settings.secret_key)
    unsigned = signer.unsign(raw.encode("utf-8"))
    padded = unsigned + b"=" * (-len(unsigned) % 4)
    return _json.loads(_b64decode(padded))


_LOGGED_IN_COOKIE = _make_session_cookie({"api_token": "test-token-locale"})


@pytest.mark.anyio
async def test_anonymous_switch_sets_cookie_and_redirects() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    ) as client:
        resp = await client.post("/set-locale", data={"locale": "ru", "next": "/some-page"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/some-page"
    assert "saebooks_locale=ru" in resp.headers.get("set-cookie", "")


@pytest.mark.anyio
async def test_unsupported_locale_is_a_noop_redirect() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    ) as client:
        resp = await client.post("/set-locale", data={"locale": "fr", "next": "/x"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/x"
    assert "saebooks_locale" not in resp.headers.get("set-cookie", "")


@pytest.mark.anyio
async def test_cross_origin_next_falls_back_to_root() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/set-locale", data={"locale": "et", "next": "https://evil.example/steal"},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


@pytest.mark.anyio
async def test_logged_in_switch_persists_to_session_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /set-locale as a logged-in user, then use the resulting session
    cookie on a fresh request and confirm the dashboard header actually
    renders in the new locale — the round trip the deliverable asks for,
    not just a claimed 303.

    SAEBOOKS_BRAND=tasur: the switcher itself (whose own "Language"/"Keel"
    chrome string this test reads back) is gated to the Tasur/EE brand —
    see fixer round 1 — so it must be exercised under that brand, not the
    stock AU/SAE Books default where the switcher no longer renders.
    """
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _LOGGED_IN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/set-locale", data={"locale": "et", "next": "/"})
    assert resp.status_code == 303

    new_session_cookie = resp.cookies.get(settings.session_cookie_name)
    assert new_session_cookie, "switching locale must re-sign the session cookie"
    decoded = _decode_session_cookie(new_session_cookie)
    assert decoded.get("locale") == "et"
    assert decoded.get("api_token") == "test-token-locale"  # rest of session untouched

    with respx.mock:
        _register_mocks(respx.mock)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={settings.session_cookie_name: new_session_cookie},
        ) as client2:
            page = await client2.get("/")
    assert page.status_code == 200
    # "Language" -> "Keel" (et) is the switcher's own chrome string; its
    # presence in the rendered page proves request.state.locale flowed all
    # the way from the session write through LocaleMiddleware to the
    # gettext call in base.html.
    assert "Keel" in page.text


@pytest.mark.anyio
async def test_no_next_field_same_origin_referer_redirects_to_referring_path() -> None:
    """No `next` field, but a same-origin Referer header (what every real
    browser actually sends -- always an absolute URL) -- must redirect
    back to the referring page's path, not fall back to "/"."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/set-locale",
            data={"locale": "ru"},
            headers={"referer": "http://test/invoices/123"},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/invoices/123"


@pytest.mark.anyio
async def test_no_next_field_cross_origin_referer_falls_back_to_root() -> None:
    """A cross-origin Referer must still fall back to "/", exactly like a
    cross-origin `next` value -- the fix only accepts same-origin
    absolute URLs, it doesn't loosen the check generally.

    In practice CSRFMiddleware's own Referer-origin check (Layer 2 —
    security/csrf.py) rejects a cross-site Referer with 403 before this
    route ever runs, so the redirect logic never actually sees the
    cross-origin value on a real POST. Prove the _safe_next unit itself
    still rejects it (defense in depth — the two checks are independent
    layers), rather than relying only on the 403 to mask a would-be gap.
    """
    from saebooks_web.routes.locale import _safe_next

    assert _safe_next(None, own_origin="http://test") == "/"
    assert (
        _safe_next("https://evil.example/steal", own_origin="http://test") == "/"
    )

    # And, end-to-end: CSRFMiddleware's independent Referer check already
    # blocks this before _safe_next's own-origin comparison would matter.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/set-locale",
            data={"locale": "ru"},
            headers={"referer": "https://evil.example/steal"},
        )
    assert resp.status_code == 403
