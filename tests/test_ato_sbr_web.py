"""Tests for the ATO SBR web views — Lane D cycle 54.

1.  test_ato_sbr_requires_auth            — GET /admin/ato-sbr without session -> 303 /login
2.  test_ato_sbr_renders                  — GET /admin/ato-sbr returns 200 with wizard
3.  test_ato_sbr_feature_disabled         — GET /admin/ato-sbr with API 404 -> feature-disabled notice
4.  test_ato_sbr_api_error                — GET /admin/ato-sbr with API 500 -> error banner
5.  test_ato_sbr_keystore_requires_auth   — POST /admin/ato-sbr/keystore without session -> 303
6.  test_ato_sbr_keystore_success         — POST with file + pass -> API 201 -> redirected
7.  test_ato_sbr_ssid_requires_auth       — POST /admin/ato-sbr/onboarding/start without session -> 303
                                             (repointed: the flat ssid/confirm/test/clear action
                                             endpoints from the pre-W3 API no longer exist — see the
                                             docstring on this test for the mapping to the current
                                             stepped onboarding wizard)
8.  test_ato_sbr_ssid_success             — POST onboarding/start -> API 201 -> redirected
9.  test_ato_sbr_confirm_success          — POST onboarding/{id}/step -> API 200 -> redirected
10. test_ato_sbr_test_success             — POST /admin/ato-sbr/ping -> API 200 -> redirected
11. test_ato_sbr_clear_success            — POST keystore/{id}/delete -> API 204 -> redirected
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
from tests import _jp

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
    """GET /admin/ato-sbr with API 200 -> wizard page rendered.

    The Cat-C (W3) rewrite fetches keystore entries from
    GET /api/v1/ato_sbr/keystore (JSON) and renders them server-side via
    templates/ato_sbr/index.html, rather than proxying raw HTML from
    /admin/ato-sbr on the API.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
        return_value=Response(200, json={"items": []})
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
    respx_mock.get(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
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
    respx_mock.get(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
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
    """POST /admin/ato-sbr/keystore with API 201 -> redirected to wizard.

    The Cat-C rewrite proxies straight to POST /api/v1/ato_sbr/keystore and
    expects a 201 with the created entry (not a 303 passthrough); the web
    route itself issues the 303 redirect back to /admin/ato-sbr with a flash.
    """
    respx_mock.post(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
        return_value=Response(201, json={"abn_or_name": "SAE"})
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
    """POST /admin/ato-sbr/onboarding/start without session -> 303 /login.

    The Cat-C (W3) rewrite deleted the flat ssid/confirm/test/clear action
    endpoints entirely and replaced them with a stepped onboarding wizard
    (POST /admin/ato-sbr/onboarding/start, POST .../onboarding/{id}/step,
    POST /admin/ato-sbr/ping). This is a feature rewrite, not harness drift:
    there is no longer a POST /admin/ato-sbr/ssid route at all (it 404s
    before an auth check can even run), so this test is repointed to the
    closest surviving auth-gated onboarding action.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/ato-sbr/onboarding/start", data={"flow": "machine_credential"}
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 8. SSID save success (repointed — see note in ssid_requires_auth above)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_ssid_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/onboarding/start with API 201 -> redirected to the wizard."""
    respx_mock.post(f"{_API_BASE}/api/v1/ato_sbr/onboarding/wizards").mock(
        return_value=Response(
            201,
            json={"wizard_id": "wiz-001", "current_step": "mygovid", "step_index": 0, "step_count": 5},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/ato-sbr/onboarding/start", data={"flow": "machine_credential"}
        )

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 9. Confirm step (repointed to the onboarding wizard step endpoint —
#    see note in ssid_requires_auth above)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_confirm_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/onboarding/{id}/step -> API 200 -> redirected."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/ato_sbr/onboarding/wizards/wiz-001/step"
    ).mock(
        return_value=Response(
            200,
            json={"status": "in_progress", "current_step": "ram", "step_index": 1, "step_count": 5},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/ato-sbr/onboarding/wiz-001/step",
            data={"current_step": "0", "mygovid_confirmed": "on"},
        )

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 10. Smoke test (repointed to the ping endpoint — see note in
#     ssid_requires_auth above)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_test_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/ping -> API 200 (ok) -> redirected with a flash message."""
    respx_mock.post(f"{_API_BASE}/api/v1/ato_sbr/ping").mock(
        return_value=Response(200, json={"ok": True, "latency_ms": 42})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/ping", data={"keystore_id": "ks-1"})

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 11. Clear config (repointed to soft-delete a keystore entry — the closest
#     surviving "remove configured credential" action; see note in
#     ssid_requires_auth above)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_clear_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/ato-sbr/keystore/{id}/delete -> API 204 -> redirected to wizard."""
    respx_mock.delete(f"{_API_BASE}/api/v1/ato_sbr/keystore/ks-1").mock(
        return_value=Response(204)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/ato-sbr/keystore/ks-1/delete")

    assert resp.status_code == 303
    assert "/admin/ato-sbr" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 12. ATO SBR nav link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_ato_sbr_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /accounts shows ATO SBR nav link in setup sub-row."""
    _jp.mock_au_context(respx_mock)
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
