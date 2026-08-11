"""Tests for the deployment-level brand config.

SAEBOOKS_BRAND selects the active brand and is injected as the
`current_brand()` Jinja global via the existing security patch
(saebooks_web.security._patch_jinja_templates → brand.register_brand_global).

As of the 2026-08-11 rebrand there are three brands and the default is Tasur:

  * ``tasur``     — default. Neutral global product brand, ``market is None``.
  * ``tasur-ee``  — Estonian market surface, ``market == "EE"``.
  * ``saebooks``  — retained AU brand, ``market == "AU"``. Pinning it is the
                    one-env-var rollback, so it keeps full test coverage.

The load-bearing tests here are the *market* ones. Before the split, two
templates gated Estonian affordances on ``brand.key != "saebooks"``; once Tasur
became the default that negative form would have switched the et/en/ru locale
switcher and the EE onboarding block on for every Australian and global
deployment. test_locale_switcher_* and test_eid_* exist to keep that from
regressing.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web import __version__
from saebooks_web.brand import current_brand
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
# The brand table itself — market is the behavioural axis, key is not.
# ---------------------------------------------------------------------------


def test_default_brand_is_neutral_tasur() -> None:
    """No SAEBOOKS_BRAND set → Tasur, and explicitly no market."""
    brand = current_brand()
    assert brand.key == "tasur"
    assert brand.name == "Tasur"
    # The neutral surface must not claim a jurisdiction: `market is None` is
    # what keeps eID, the locale switcher and the EE onboarding block off.
    assert brand.market is None
    assert "Estonian" not in brand.meta_description
    assert not any("KMD" in f or "Käibemaks" in f for f in brand.demo_features)


@pytest.mark.parametrize(
    ("env", "key", "market"),
    [
        ("tasur", "tasur", None),
        ("tasur-ee", "tasur-ee", "EE"),
        ("saebooks", "saebooks", "AU"),
        ("TASUR-EE", "tasur-ee", "EE"),  # case-insensitive, per _current_brand_key
    ],
)
def test_brand_selection(monkeypatch: pytest.MonkeyPatch, env: str, key: str, market: str | None) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", env)
    brand = current_brand()
    assert brand.key == key
    assert brand.market == market


def test_tasur_ee_carries_the_estonian_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Estonian wording lives on tasur-ee, never on the neutral brand."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur-ee")
    brand = current_brand()
    assert brand.meta_description == "API-first accounting for Estonian small business."
    assert "Käibemaks & KMD ready" in brand.demo_features


def test_tasur_and_tasur_ee_share_the_selge_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Splitting the market must not fork the identity."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    neutral = current_brand()
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur-ee")
    ee = current_brand()
    for field in ("wordmark_src", "wordmark_src_dark", "favicon_svg", "apple_touch_icon", "accent_dark"):
        assert getattr(neutral, field) == getattr(ee, field), field


# ---------------------------------------------------------------------------
# Unauthenticated pages — /login, /signup (full <head> + auth content block,
# no sidebar since that's gated on request.session api_token).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_default_brand_is_tasur() -> None:
    """No SAEBOOKS_BRAND set → /login renders Tasur, with no SAE Books left."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert resp.status_code == 200
    assert "<title>Sign in — Tasur</title>" in resp.text
    assert "Enter your Tasur email address and password." in resp.text
    assert "New to Tasur?" in resp.text
    assert 'content="Tasur"' in resp.text  # application-name / apple-mobile-web-app-title
    assert "SAE Books" not in resp.text


@pytest.mark.anyio
async def test_login_brand_saebooks_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """SAEBOOKS_BRAND=saebooks is the rollback path and must still work fully."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert resp.status_code == 200
    assert "<title>Sign in — SAE Books</title>" in resp.text
    assert "Enter your SAE Books email address and password." in resp.text
    assert "New to SAE Books?" in resp.text
    assert 'content="SAE Books"' in resp.text
    assert "Tasur" not in resp.text


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
async def test_base_head_meta_default_vs_saebooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """<head> meta/favicon hooks swap with the brand; default is now Tasur."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        default_resp = await client.get("/login")

    assert '<meta name="description" content="API-first accounting for small business.">' in default_resp.text
    assert '<link rel="icon" type="image/png" sizes="32x32" href="/static/brand/tasur-favicon.png">' in default_resp.text
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/brand/tasur-apple-touch-icon.png">' in default_resp.text
    # Selge ships a vector favicon.
    assert '<link rel="icon" type="image/svg+xml" href="/static/brand/tasur-icon.svg">' in default_resp.text

    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sae_resp = await client.get("/login")

    assert '<meta name="description" content="API-first accounting for Australian small business.">' in sae_resp.text
    assert '<link rel="icon" type="image/png" sizes="32x32" href="/static/pwa/icons/favicon-32.png">' in sae_resp.text
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/pwa/icons/apple-touch-icon-180.png">' in sae_resp.text
    # The SAE Books brand has no vector favicon and must not emit the <link>.
    assert 'type="image/svg+xml"' not in sae_resp.text


@pytest.mark.anyio
async def test_head_meta_tasur_ee_is_estonian(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Estonian description belongs to tasur-ee alone."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur-ee")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert '<meta name="description" content="API-first accounting for Estonian small business.">' in resp.text


# ---------------------------------------------------------------------------
# Authenticated page — sidebar wordmark + command-palette footer are gated
# on request.session['api_token'] (base.html), so hit a simple authenticated
# route (mirrors tests/test_accounts.py's session-cookie pattern).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_sidebar_wordmark_and_footer(
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
    # Selge identity: two theme-scoped SVG wordmarks, CSS picks by theme.
    assert '<img src="/static/brand/tasur-wordmark-light.svg"\n           alt="Tasur"' in default_resp.text
    assert '<img src="/static/brand/tasur-wordmark-dark.svg"\n           alt="Tasur"' in default_resp.text
    assert "tasur-wordmark.png" not in default_resp.text
    assert ">Tasur · ⌘K<" in default_resp.text

    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        sae_resp = await client.get("/accounts")

    assert sae_resp.status_code == 200
    assert '<img src="/static/sae-books-logo.png"\n           alt="SAE Books"' in sae_resp.text
    assert ">SAE Books · ⌘K<" in sae_resp.text


@pytest.mark.anyio
@respx.mock
async def test_sidebar_version_badge_is_live(respx_mock: respx.MockRouter) -> None:
    """The badge renders app_version(), not the hardcoded v2026.05 it replaced.

    That literal was frozen since May 2026 and shipped stale to Richard's books
    and both demos; this is the regression guard.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert f"v{__version__} ·" in resp.text
    assert "v2026.05" not in resp.text


# ---------------------------------------------------------------------------
# Market-gated affordances. These are the tests that stop the rebrand from
# leaking Estonian UI onto Australian and global deployments.
# ---------------------------------------------------------------------------


async def _locale_switcher_absent(respx_mock: respx.MockRouter, brand_env: str) -> None:
    """Shared body: assert the et/en/ru switcher does not render for a brand.

    Not parametrized — respx.mock's wrapper masks extra arguments from pytest's
    fixture resolution, so the two callers below are spelled out instead.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert 'id="locale-switcher"' not in resp.text
    assert "Русский" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_locale_switcher_hidden_on_neutral_default(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the pre-split gate `brand.key != "saebooks"`, which
    would have rendered the Estonian switcher on the neutral global default."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    await _locale_switcher_absent(respx_mock, "tasur")


@pytest.mark.anyio
@respx.mock
async def test_locale_switcher_hidden_on_au(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    await _locale_switcher_absent(respx_mock, "saebooks")


@pytest.mark.anyio
@respx.mock
async def test_locale_switcher_shown_on_ee_market(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS_RESPONSE)
    )
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur-ee")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert 'id="locale-switcher"' in resp.text
    assert "Русский" in resp.text


@pytest.mark.parametrize(
    ("brand_env", "expected"),
    [("tasur", False), ("saebooks", False), ("tasur-ee", True)],
)
def test_eid_follows_market_not_brand(
    monkeypatch: pytest.MonkeyPatch, brand_env: str, expected: bool
) -> None:
    """Estonian eID sign-in is gated on the EE market, not on the Tasur name."""
    monkeypatch.setenv("SAEBOOKS_BRAND", brand_env)
    monkeypatch.delenv("SAEBOOKS_EID_UI", raising=False)
    monkeypatch.setattr("saebooks_web.eid_sso.is_feature_enabled", lambda _f: True)

    from saebooks_web.eid_sso import eid_enabled

    assert eid_enabled() is expected


def test_eid_override_env_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """SAEBOOKS_EID_UI remains the explicit escape hatch on any brand."""
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    monkeypatch.setenv("SAEBOOKS_EID_UI", "1")
    monkeypatch.setattr("saebooks_web.eid_sso.is_feature_enabled", lambda _f: True)

    from saebooks_web.eid_sso import eid_enabled

    assert eid_enabled() is True


@pytest.mark.anyio
async def test_unknown_brand_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "not-a-real-brand")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")

    assert resp.status_code == 200
    assert "<title>Sign in — Tasur</title>" in resp.text
