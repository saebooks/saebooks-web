"""Tests for the ATO SBR web views — Lane D cycle 54.

1.  test_ato_sbr_requires_auth            — GET /admin/ato-sbr without session -> 303 /login
2.  test_ato_sbr_renders                  — GET /admin/ato-sbr returns 200 with wizard
3.  test_ato_sbr_feature_disabled         — GET /admin/ato-sbr with API 404 -> feature-disabled notice
4.  test_ato_sbr_api_error                — GET /admin/ato-sbr with API 500 -> error banner
5.  test_ato_sbr_keystore_requires_auth   — POST /admin/ato-sbr/keystore without session -> 303
6.  test_ato_sbr_keystore_success         — POST with file + pass -> API 303 -> redirected
7.  test_ato_sbr_ssid_requires_auth       — POST /admin/ato-sbr/ssid without session -> 303
8.  test_ato_sbr_ssid_success             — POST ssid -> API 303 -> redirected
9.  test_ato_sbr_confirm_success          — POST confirm -> API 303 -> redirected
10. test_ato_sbr_test_success             — POST test env -> API 303 -> redirected
11. test_ato_sbr_clear_success            — POST clear -> API 303 -> redirected
12. test_ato_sbr_nav_link                 — GET /accounts shows ATO SBR nav link
13. test_ato_sbr_keystore_get_anon        — GET /admin/ato-sbr/keystore anon -> 303 /login (no two-hop)
14. test_ato_sbr_keystore_get_forbidden   — GET /admin/ato-sbr/keystore bookkeeper -> 403 direct
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
# Constants / helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")

_WIZARD_HTML = """
<div class="sbr-wizard">
  <h1>ATO SBR Setup</h1>
  <p>Status: not configured</p>
  <form method="post" action="/admin/ato-sbr/keystore" enctype="multipart/form-data">
    <input type="file" name="file">
    <input type="password" name="password">
    <button type="submit">Upload Keystore</button>
  </form>
</div>
"""


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-atosbr", "user_role": "admin"})


# ---------------------------------------------------------------------------
# 1. Auth gate — GET /admin/ato-sbr
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ato_sbr_requires_auth() -> None:
    """GET /admin/ato-sbr without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/ato-sbr")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Renders wizard when API available
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_renders(respx_mock: respx.MockRouter) -> None:
    """GET /admin/ato-sbr with API 200 -> wizard page rendered."""
    respx_mock.get(f"{_API_BASE}/admin/ato-sbr").mock(
        return_value=Response(
            200,
            content=_WIZARD_HTML.encode(),
            headers={"content-type": "text/html"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/ato-sbr")

    assert resp.status_code == 200
    assert "ATO SBR" in resp.text
    assert "Keystore" in resp.text or "keystore" in resp.text.lower()


# ---------------------------------------------------------------------------
# 3. Feature disabled — API 404
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_feature_disabled(respx_mock: respx.MockRouter) -> None:
    """GET /admin/ato-sbr with API 404 -> feature-disabled notice shown."""
    respx_mock.get(f"{_API_BASE}/admin/ato-sbr").mock(
        return_value=Response(404, json={"detail": "Feature not enabled"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/ato-sbr")

    assert resp.status_code == 200
    assert "not enabled" in resp.text.lower() or "feature" in resp.text.lower()


# ---------------------------------------------------------------------------
# 4. API error — shows error banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_api_error(respx_mock: respx.MockRouter) -> None:
    """GET /admin/ato-sbr with API 500 -> error banner shown."""
    respx_mock.get(f"{_API_BASE}/admin/ato-sbr").mock(
        return_value=Response(500, json={"detail": "Internal error"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/ato-sbr")

    assert resp.status_code == 200
    assert "API error" in resp.text or "500" in resp.text


# ---------------------------------------------------------------------------
# 5. Keystore upload auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ato_sbr_keystore_requires_auth() -> None:
    """POST /admin/ato-sbr/keystore without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/keystore", data={"password": "test"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 6. Keystore upload success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_keystore_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/keystore with API 303 -> redirected to wizard."""
    respx_mock.post(f"{_API_BASE}/admin/ato-sbr/keystore").mock(
        return_value=Response(
            303,
            headers={"location": "/admin/ato-sbr?message=keystore+loaded+(CN=SAE)"},
        )
    )

    ks_xml = b"<keystore><entry><alias>key</alias></entry></keystore>"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/ato-sbr/keystore",
            files={"file": ("keystore.xml", ks_xml, "application/xml")},
            data={"password": "s3cr3t"},
        )

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 7. SSID auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ato_sbr_ssid_requires_auth() -> None:
    """POST /admin/ato-sbr/ssid without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/ssid", data={"ssid": "SB12345678"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 8. SSID save success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_ssid_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/ssid with API 303 -> redirected to wizard."""
    respx_mock.post(f"{_API_BASE}/admin/ato-sbr/ssid").mock(
        return_value=Response(
            303,
            headers={"location": "/admin/ato-sbr?message=ssid+saved"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/ssid", data={"ssid": "SB12345678"})

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 9. Confirm step
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_confirm_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/confirm -> API 303 -> redirected."""
    respx_mock.post(f"{_API_BASE}/admin/ato-sbr/confirm").mock(
        return_value=Response(
            303,
            headers={"location": "/admin/ato-sbr?message=step+mygovid+confirmed"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/confirm", data={"step": "mygovid"})

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 10. Smoke test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_test_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/test -> API 303 -> redirected with test result."""
    respx_mock.post(f"{_API_BASE}/admin/ato-sbr/test").mock(
        return_value=Response(
            303,
            headers={"location": "/admin/ato-sbr?test=ok&test_env=EVTE&message=connection+ok"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/test", data={"environment": "EVTE"})

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 11. Clear config
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_clear_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/clear -> API 303 -> redirected to wizard."""
    respx_mock.post(f"{_API_BASE}/admin/ato-sbr/clear").mock(
        return_value=Response(
            303,
            headers={"location": "/admin/ato-sbr?message=config+cleared"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/clear")

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 12. ATO SBR nav link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /accounts shows ATO SBR nav link in setup sub-row."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert "/admin/ato-sbr" in resp.text
    assert "ATO SBR" in resp.text


# ---------------------------------------------------------------------------
# 13. GET /admin/ato-sbr/keystore — anonymous -> 303 /login (audit-trail #10 A.4)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ato_sbr_keystore_get_anon() -> None:
    """GET /admin/ato-sbr/keystore without session -> 303 /login directly.

    Regression guard: this path MUST NOT return 303 -> /admin/ato-sbr -> 403;
    anonymous users should land on the login page, not an intermediate admin
    page.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/ato-sbr/keystore")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 14. GET /admin/ato-sbr/keystore — bookkeeper -> 403 direct (audit-trail #10 A.4)
# ---------------------------------------------------------------------------

_BOOKKEEPER_COOKIE_ATB = _make_session_cookie(
    {"api_token": "test-token-bk-atb", "is_sae_staff": False, "user_role": "bookkeeper"}
)


@pytest.mark.anyio
async def test_ato_sbr_keystore_get_forbidden() -> None:
    """GET /admin/ato-sbr/keystore with bookkeeper session -> 403 directly.

    Regression guard for audit-trail #10 Probe A.4: previously returned
    303 -> /admin/ato-sbr -> 403 (two hops, inconsistent with every other
    /admin/* route that returns 403 directly).
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _BOOKKEEPER_COOKIE_ATB},
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/ato-sbr/keystore")

    assert resp.status_code == 403, (
        f"Expected direct 403 for bookkeeper on GET /admin/ato-sbr/keystore, "
        f"got {resp.status_code}"
    )
