"""Tests for CSRF Layer 2 — OriginRefererMiddleware.

Verifies that:
    - Cross-origin POSTs (Origin mismatch) are rejected with 403 +
      ``code: cross_origin_forbidden``.
    - Cross-origin POSTs (Referer mismatch when Origin absent) are rejected.
    - Same-origin POSTs (Origin matches site origin) pass through.
    - GET requests are never rejected even with a foreign Origin (semantics
      require GETs to be safe).
    - /api/v1/* prefix is exempt (no such routes today; defence for future
      JSON-bearer routes).
    - /healthz is exempt (liveness probe).
"""
from __future__ import annotations

import os

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks_web.config import settings
from saebooks_web.main import app
from tests.test_smoke import _make_session_cookie

_API_BASE = settings.api_url.rstrip("/")


def _session_cookie() -> str:
    return _make_session_cookie({"api_token": "test-token-csrf-l2"})


@pytest.mark.anyio
async def test_post_with_foreign_origin_is_rejected() -> None:
    """A POST from attacker.example.com is rejected as cross_origin_forbidden."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            headers={
                "Origin": "https://attacker.example.com",
                "Referer": "http://test/",
            },
            data={"name": "PWNED", "contact_type": "CUSTOMER"},
        )

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "cross_origin_forbidden"


@pytest.mark.anyio
async def test_post_with_foreign_referer_only_is_rejected() -> None:
    """A POST with no Origin but cross-site Referer is also rejected."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            headers={"Referer": "https://attacker.example.com/page"},
            data={"name": "PWNED", "contact_type": "CUSTOMER"},
        )

    assert resp.status_code == 403
    assert resp.json()["code"] == "cross_origin_forbidden"


@pytest.mark.anyio
@respx.mock
async def test_post_with_matching_origin_passes(respx_mock: respx.MockRouter) -> None:
    """A POST with an Origin matching the configured site origin is allowed."""
    # Default test site origin is http://test (set in conftest.py).
    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(201, json={
            "id": "00000000-0000-0000-0000-000000000abc",
            "name": "Acme",
            "contact_type": "CUSTOMER",
            "version": 1,
        })
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            headers={
                "Origin": "http://test",
                "Referer": "http://test/contacts/new",
            },
            data={
                "name": "Acme",
                "contact_type": "CUSTOMER",
                "idempotency_key": "11111111-1111-1111-1111-111111111111",
            },
        )

    # Either the API mock returns 201 -> 303, or whatever real flow.
    # The key assertion is that we do NOT get 403 from the CSRF middleware.
    assert resp.status_code != 403


@pytest.mark.anyio
async def test_get_is_never_rejected_even_cross_origin() -> None:
    """A GET with a foreign Origin still works — GETs must remain safe."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(
            "/healthz",
            headers={"Origin": "https://attacker.example.com"},
        )

    assert resp.status_code == 200


@pytest.mark.anyio
async def test_healthz_post_skipped() -> None:
    """The /healthz path is in the skip list — even POST passes through."""
    # /healthz only handles GET; POST returns 405.  But it must NOT be 403
    # (CSRF rejection); otherwise we'd leak that the path is CSRF-protected.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/healthz",
            headers={"Origin": "https://attacker.example.com"},
        )

    assert resp.status_code in (404, 405)  # not 403


@pytest.mark.anyio
async def test_post_without_origin_or_referer_passes() -> None:
    """Missing both Origin and Referer is logged but not rejected by Layer 2.

    This is the test-harness shape; Layer 3 (CSRF token) handles real
    enforcement.  Without Layer 3 wired yet, the request reaches the route
    handler — which will return whatever it would return for the request's
    payload (likely 422 for invalid form).
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={"name": "x", "contact_type": "CUSTOMER"},
        )

    # Not 403 — Layer 2 doesn't reject missing-both.
    assert resp.status_code != 403


@pytest.mark.anyio
async def test_null_origin_is_treated_as_absent() -> None:
    """Some clients send literal Origin: null — treat as absent, not foreign."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            headers={"Origin": "null", "Referer": "http://test/"},
            data={"name": "x", "contact_type": "CUSTOMER"},
        )
    # Should proceed to the route (Referer matches site origin), not 403.
    assert resp.status_code != 403


@pytest.mark.anyio
async def test_site_origin_is_configurable_via_env() -> None:
    """Per-call env override changes the trusted site origin.

    We exercise the env-var path by temporarily flipping
    SAEBOOKS_WEB_SITE_ORIGIN and asserting that what was previously
    same-origin (http://test) becomes cross-origin.
    """
    original = os.environ.get("SAEBOOKS_WEB_SITE_ORIGIN")
    os.environ["SAEBOOKS_WEB_SITE_ORIGIN"] = "https://other.example.com"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={settings.session_cookie_name: _session_cookie()},
            follow_redirects=False,
        ) as client:
            resp = await client.post(
                "/contacts/new",
                headers={"Origin": "http://test"},
                data={"name": "x", "contact_type": "CUSTOMER"},
            )
        assert resp.status_code == 403
        assert resp.json()["code"] == "cross_origin_forbidden"
    finally:
        if original is None:
            os.environ.pop("SAEBOOKS_WEB_SITE_ORIGIN", None)
        else:
            os.environ["SAEBOOKS_WEB_SITE_ORIGIN"] = original
