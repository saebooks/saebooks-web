"""Regression tests — P0-2: all /admin/* web routes must gate non-staff/non-admin.

Covers audit-trail #05 P0-2: chen_apex (bookkeeper) was able to reach
/admin/audit and /admin/sql-tool. The SQL Tool was gated in b7c7576.
This file covers the remaining staff-only and admin-only routes.

Staff-only routes (is_sae_staff required):
  GET  /admin/audit

Admin-role routes (user_role == "admin" or is_sae_staff):
  GET  /admin/imports/
  GET  /admin/imports/bank
  POST /admin/imports/bank/preview
  POST /admin/imports/bank/apply
  GET  /admin/imports/coa
  POST /admin/imports/coa/preview
  POST /admin/imports/coa/apply
  GET  /admin/ranges
  GET  /admin/ranges/new
  POST /admin/ranges/new
  POST /admin/ranges/prefix_mode
  GET  /admin/ranges/{id}/edit
  POST /admin/ranges/{id}/edit
  POST /admin/ranges/{id}/delete
  GET  /admin/ato-sbr
  POST /admin/ato-sbr/keystore
  POST /admin/ato-sbr/onboarding/start
  POST /admin/ato-sbr/ping
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
# Session cookie helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


# Bookkeeper — authenticated but neither SAE staff nor admin role.
_BOOKKEEPER_COOKIE = _make_session_cookie(
    {"api_token": "test-token-bk", "is_sae_staff": False, "user_role": "bookkeeper"}
)

# Tenant admin — authenticated, admin role, NOT SAE staff.
_TENANT_ADMIN_COOKIE = _make_session_cookie(
    {"api_token": "test-token-ta", "is_sae_staff": False, "user_role": "admin"}
)

# SAE staff — authenticated, is_sae_staff=True.
_STAFF_COOKIE = _make_session_cookie(
    {"api_token": "test-token-staff", "is_sae_staff": True, "user_role": "admin"}
)


# ---------------------------------------------------------------------------
# Staff-only routes: GET /admin/audit
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_audit_forbidden_for_bookkeeper(respx_mock: respx.MockRouter) -> None:
    """Bookkeeper must get 403 on GET /admin/audit (P0-2 regression)."""
    respx_mock.get(f"{_API_BASE}/admin/audit").mock(
        return_value=Response(200, content=b"<html>audit</html>",
                              headers={"content-type": "text/html"})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 403, f"Expected 403 for bookkeeper on /admin/audit, got {resp.status_code}"


@pytest.mark.anyio
@respx.mock
async def test_audit_forbidden_for_tenant_admin(respx_mock: respx.MockRouter) -> None:
    """Tenant admin (non-staff) must get 403 on GET /admin/audit."""
    respx_mock.get(f"{_API_BASE}/admin/audit").mock(
        return_value=Response(200, content=b"<html>audit</html>",
                              headers={"content-type": "text/html"})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _TENANT_ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 403, f"Expected 403 for tenant admin on /admin/audit, got {resp.status_code}"


@pytest.mark.anyio
@respx.mock
async def test_audit_allowed_for_staff(respx_mock: respx.MockRouter) -> None:
    """SAE staff must get 200 on GET /admin/audit."""
    respx_mock.get(f"{_API_BASE}/api/v1/admin/audit-log").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _STAFF_COOKIE},
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Admin-role routes: parametrised 403 check for bookkeeper
#
# We test the GET routes that don't need complex API mock setup, plus
# selected POST routes. A bookkeeper (non-admin, non-staff) must get 403
# on all of them. Tenant admins must get through (200 or redirect).
# ---------------------------------------------------------------------------

_ADMIN_GET_ROUTES = [
    "/admin/imports/",
    "/admin/imports/bank",
    "/admin/imports/coa",
    "/admin/ranges",
    "/admin/ranges/new",
    "/admin/ato-sbr",
]

_ADMIN_POST_ROUTES_EMPTY_BODY: list[str] = [
    "/admin/ranges/prefix_mode",
    # Cat-C rewrite: legacy /ssid, /confirm, /test, /clear endpoints replaced
    # by the wizard surface (/onboarding/start, /onboarding/{id}/step) and /ping.
    "/admin/ato-sbr/onboarding/start",
    "/admin/ato-sbr/ping",
]


@pytest.mark.anyio
@pytest.mark.parametrize("path", _ADMIN_GET_ROUTES)
@respx.mock
async def test_admin_get_forbidden_for_bookkeeper(
    path: str, respx_mock: respx.MockRouter
) -> None:
    """Bookkeeper must get 403 on every admin GET route."""
    # Stub out any upstream GET the route might call before our auth check.
    # Cat-C rewrite: ato_sbr now calls /api/v1/ato_sbr/keystore.
    respx_mock.get(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get(path)

    assert resp.status_code == 403, (
        f"Expected 403 for bookkeeper on GET {path}, got {resp.status_code}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("path", _ADMIN_POST_ROUTES_EMPTY_BODY)
async def test_admin_post_forbidden_for_bookkeeper(path: str) -> None:
    """Bookkeeper must get 403 on admin POST routes (no upstream call needed)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(path, data={})

    assert resp.status_code == 403, (
        f"Expected 403 for bookkeeper on POST {path}, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Tenant admin passes the role gate (allowed through, then proxied to API).
# We just need to confirm the web layer doesn't 403 them.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_landing_allowed_for_tenant_admin(
    respx_mock: respx.MockRouter,
) -> None:
    """Tenant admin must NOT get 403 on GET /admin/imports/."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _TENANT_ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/imports/")

    # 200 (rendered template) or 303 (any redirect) — neither is a 403.
    assert resp.status_code != 403, (
        f"Tenant admin should not be 403'd on /admin/imports/, got {resp.status_code}"
    )


@pytest.mark.anyio
@respx.mock
async def test_ranges_list_allowed_for_tenant_admin(
    respx_mock: respx.MockRouter,
) -> None:
    """Tenant admin must NOT get 403 on GET /admin/ranges."""
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges").mock(
        return_value=Response(200, json=[])
    )
    respx_mock.get(f"{_API_BASE}/api/v1/account_ranges/prefix_mode").mock(
        return_value=Response(200, json={"prefix_mode": "none"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _TENANT_ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/ranges")

    assert resp.status_code != 403, (
        f"Tenant admin should not be 403'd on /admin/ranges, got {resp.status_code}"
    )


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_allowed_for_tenant_admin(
    respx_mock: respx.MockRouter,
) -> None:
    """Tenant admin must NOT get 403 on GET /admin/ato-sbr."""
    # Cat-C rewrite: route now calls /api/v1/ato_sbr/keystore (returns JSON
    # list) instead of proxying upstream /admin/ato-sbr HTML.
    respx_mock.get(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
        return_value=Response(200, json={"items": []})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _TENANT_ADMIN_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/ato-sbr")

    assert resp.status_code != 403, (
        f"Tenant admin should not be 403'd on /admin/ato-sbr, got {resp.status_code}"
    )
