"""Tests for the plan-confirmation interstitial gate — D/billing-gate.

Ensures that:
1. GET /billing/checkout without a session redirects to /login (not Stripe).
2. GET /billing/checkout?plan=business with a session renders the confirmation
   page with plan details and a "Continue to payment" button — NOT an auto-submit.
3. GET /billing/checkout?plan=pro with a session renders Pro plan details.
4. GET /billing/checkout?plan=unknown with a session redirects to /billing/upgrade
   (no valid plan).
5. POST /billing/checkout (the form submit from the interstitial) with a valid
   session calls the API and 303-redirects to the Stripe URL returned.
6. The confirm_plan.html page does NOT contain the auto-submit JavaScript
   that was in checkout_redirect.html.
"""
from __future__ import annotations

import json
from base64 import b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = TimestampSigner(settings.secret_key)
    payload = b64encode(json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-billing-gate"})


# ---------------------------------------------------------------------------
# 1. Unauthenticated visitor — must go to /login, not Stripe
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_checkout_page_redirects_unauthed_to_login() -> None:
    """GET /billing/checkout without session -> 303 to /login?next=..."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/billing/checkout?plan=business")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/login")
    assert "billing/checkout" in location
    # Must NOT redirect directly to Stripe
    assert "stripe.com" not in location
    assert "buy.stripe" not in location


# ---------------------------------------------------------------------------
# 2. Authenticated visitor — business plan shows confirmation interstitial
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_checkout_page_shows_confirm_interstitial_business() -> None:
    """GET /billing/checkout?plan=business with session renders confirm_plan page."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/billing/checkout?plan=business")

    assert resp.status_code == 200
    body = resp.text
    # Must show plan name and CTA — not auto-submit to Stripe
    assert "Business" in body
    assert "Continue to payment" in body
    # Must NOT contain the old auto-submit JavaScript
    assert 'document.getElementById("checkout-form").submit()' not in body
    # Must show price info
    assert "$49" in body


# ---------------------------------------------------------------------------
# 3. Authenticated visitor — pro plan shows Pro details
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_checkout_page_shows_confirm_interstitial_pro() -> None:
    """GET /billing/checkout?plan=pro with session renders Pro plan details."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/billing/checkout?plan=pro")

    assert resp.status_code == 200
    body = resp.text
    assert "Pro" in body
    assert "Continue to payment" in body
    assert "$99" in body


# ---------------------------------------------------------------------------
# 4. Unknown plan redirects to /billing/upgrade
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_checkout_page_unknown_plan_redirects_to_upgrade() -> None:
    """GET /billing/checkout?plan=free with session -> 303 to /billing/upgrade."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/billing/checkout?plan=free")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/billing/upgrade"


# ---------------------------------------------------------------------------
# 5. POST /billing/checkout (from the interstitial form) -> Stripe redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_checkout_post_redirects_to_stripe(
    respx_mock: respx.MockRouter,
) -> None:
    """POST /billing/checkout with valid session and edition -> 303 to Stripe URL."""
    _STRIPE_URL = "https://checkout.stripe.com/pay/cs_test_abc123"
    respx_mock.post(f"{_API_BASE}/api/v1/billing/checkout-session").mock(
        return_value=Response(200, json={"checkout_url": _STRIPE_URL})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/billing/checkout",
            data={"edition": "business"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == _STRIPE_URL


# ---------------------------------------------------------------------------
# 6. POST /billing/checkout without session -> back to /login
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_checkout_post_unauthed_redirects_to_login() -> None:
    """POST /billing/checkout without session -> 303 to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/billing/checkout",
            data={"edition": "business"},
        )

    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]
