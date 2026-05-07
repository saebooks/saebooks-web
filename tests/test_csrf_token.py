"""Tests for CSRF Layer 3 — per-form CSRF token (CSRFMiddleware).

Layer 3 enforces a per-session CSRF token on every state-changing form
submission.  The token lives in ``request.session['csrf_token']`` (lazy-
generated) and must match the ``csrf_token`` field in the submitted form.

Conftest behaviour
------------------
``conftest.py`` injects a fixed token into both the session cookie and the
form body for legacy tests.  Tests in this file that want to exercise the
*rejection* path explicitly submit a token (correct or wrong) so the
auto-injector skips them — see the ``data={...}`` dicts below.

Cases covered
-------------
- POST with no token field           → 403 csrf_token_mismatch
- POST with wrong token              → 403 csrf_token_mismatch
- POST with correct token            → reaches the handler (not 403)
- POST with no session (anonymous)   → falls through to auth (303 to /login)
- /login is exempt from Layer 3      → the credentials POST is not 403
- /healthz is skipped by Layer 3     → the GET still 200
- HTMX POST without token            → 403 csrf_token_mismatch
"""
from __future__ import annotations

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from conftest import TEST_CSRF_TOKEN
from saebooks_web.config import settings
from saebooks_web.main import app
from tests.test_smoke import _make_session_cookie

_API_BASE = settings.api_url.rstrip("/")


def _session_cookie() -> str:
    """Authenticated session — conftest will inject TEST_CSRF_TOKEN."""
    return _make_session_cookie({"api_token": "test-token-csrf-l3"})


@pytest.mark.anyio
async def test_post_without_token_is_rejected() -> None:
    """A logged-in POST that submits *no* csrf_token returns 403.

    We pre-populate the body with an empty csrf_token to suppress the
    conftest auto-injector — the middleware then sees an empty token,
    fails the compare, and rejects.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={"name": "x", "contact_type": "CUSTOMER", "csrf_token": ""},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "csrf_token_mismatch"


@pytest.mark.anyio
async def test_post_with_wrong_token_is_rejected() -> None:
    """A logged-in POST with the wrong csrf_token returns 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={
                "name": "x",
                "contact_type": "CUSTOMER",
                "csrf_token": "wrong-token-not-the-fixed-one",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf_token_mismatch"


@pytest.mark.anyio
@respx.mock
async def test_post_with_correct_token_passes(respx_mock: respx.MockRouter) -> None:
    """A logged-in POST with the correct csrf_token reaches the handler."""
    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(201, json={
            "id": "00000000-0000-0000-0000-000000000abc",
            "name": "Acme",
            "contact_type": "CUSTOMER",
            "version": 1,
        }),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={
                "name": "Acme",
                "contact_type": "CUSTOMER",
                "csrf_token": TEST_CSRF_TOKEN,
                "idempotency_key": "11111111-1111-1111-1111-111111111111",
            },
        )
    # We don't assert the exact downstream code; only that CSRF didn't reject.
    assert resp.status_code != 403


@pytest.mark.anyio
async def test_anonymous_post_falls_through_to_auth() -> None:
    """A POST without a session cookie skips Layer 3 and the auth check redirects."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={"name": "x", "contact_type": "CUSTOMER"},
        )
    # Auth check redirects to /login (303), not Layer 3 rejection (403).
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/login"


@pytest.mark.anyio
async def test_login_is_exempt_from_layer_3() -> None:
    """POST /login is in _TOKEN_SKIP_PATHS — no token required.

    /login is the bootstrap: there is no session yet to bind a token to.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        # Submit form with no csrf_token field.
        resp = await client.post(
            "/login",
            data={"username": "x", "password": "y", "csrf_token": ""},
        )
    # Whatever auth says (probably 401/redirect), Layer 3 must NOT reject.
    assert resp.status_code != 403 or resp.json().get("code") != "csrf_token_mismatch"


@pytest.mark.anyio
async def test_get_healthz_is_skipped() -> None:
    """GET /healthz never sees Layer 3 (skipped path)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_htmx_post_without_token_is_rejected() -> None:
    """HTMX form POSTs are bound by the same rules — no token = 403.

    HTMX forms include ``hx-post`` instead of ``method=POST``; the request
    still arrives as POST with content-type x-www-form-urlencoded, so the
    middleware treats them identically.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _session_cookie()},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/sql-tool/execute",
            headers={"HX-Request": "true"},
            data={"sql": "SELECT 1", "csrf_token": ""},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf_token_mismatch"
