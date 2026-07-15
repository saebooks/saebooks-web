"""Module-gate degrade layer (M2 app-lane step 7) — tests.

Covers the acceptance bar from the app-lane spec:

1.  Connection error on an API call → 503 + degraded panel (not a 500).
2.  Read timeout → same.
3.  Engine guarded-import stub 503 ({"status": "unavailable", "module": X})
    → degraded panel, module id surfaced.
4.  Delegated-service RFC 7807 503 ({"code": "module_unavailable", ...})
    → degraded panel (shape 2).
5.  A differently-shaped business-logic 503 (ato_sbr.py's "Encryption not
    configured") does NOT trigger the degrade layer — the existing flash
    redirect fires, byte-for-byte unchanged.
6.  _is_module_unavailable_503 unit cases (non-JSON, non-dict, wrong code).
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app
from saebooks_web.module_gate import _is_module_unavailable_503

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-gate", "user_role": "admin", "locale": "en"}
)


def _client(**kw) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        **kw,
    )


# ---------------------------------------------------------------------------
# 1/2. Connection-level failures → degraded panel at 503
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_connect_error_renders_degraded_panel(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with _client() as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 503
    assert "data-degraded-panel" in resp.text
    assert "temporarily unavailable" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_read_timeout_renders_degraded_panel(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    async with _client() as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 503
    assert "data-degraded-panel" in resp.text


# ---------------------------------------------------------------------------
# 3/4. The two engine module-unavailable 503 shapes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_guarded_import_stub_503_renders_degraded_panel(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(
            503, json={"module": "bank_feeds", "status": "unavailable"}
        )
    )

    async with _client() as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 503
    assert "data-degraded-panel" in resp.text
    assert 'data-module-id="bank_feeds"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_delegated_rfc7807_503_renders_degraded_panel(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(
            503,
            json={
                "status": 503,
                "code": "module_unavailable",
                "module": "capture",
                "title": "Module unavailable",
            },
            headers={"content-type": "application/problem+json"},
        )
    )

    async with _client() as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 503
    assert 'data-module-id="capture"' in resp.text


# ---------------------------------------------------------------------------
# 5. Business-logic 503 passes through untouched (ato_sbr keystore)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_business_503_does_not_trigger_degrade(
    respx_mock: respx.MockRouter,
) -> None:
    """ato_sbr.py's legitimate 503 ("Encryption not configured") must keep
    flowing into the existing flash-redirect branch, not the degraded
    panel."""
    respx_mock.post(f"{_API_BASE}/api/v1/ato_sbr/keystore").mock(
        return_value=Response(
            503, json={"detail": "Encryption not configured on server"}
        )
    )

    ks_xml = b"<keystore><entry><alias>key</alias></entry></keystore>"
    async with _client(follow_redirects=False) as client:
        resp = await client.post(
            "/admin/ato-sbr/keystore",
            files={"file": ("keystore.xml", ks_xml, "application/xml")},
            data={"password": "s3cr3t"},
        )

    # Existing behaviour: flash + 303 back to the wizard — NOT a 503 page.
    assert resp.status_code == 303
    assert "data-degraded-panel" not in resp.text


# ---------------------------------------------------------------------------
# 6. Shape-classifier unit cases
# ---------------------------------------------------------------------------


def _resp(status: int, content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status, content=content, headers={"content-type": content_type}
    )


def test_classifier_matches_guarded_import_stub() -> None:
    r = _resp(503, b'{"status": "unavailable", "module": "stp"}', "application/json")
    assert _is_module_unavailable_503(r) == "stp"


def test_classifier_matches_rfc7807() -> None:
    r = _resp(
        503,
        b'{"status": 503, "code": "module_unavailable", "module": "platform"}',
        "application/problem+json",
    )
    assert _is_module_unavailable_503(r) == "platform"


def test_classifier_ignores_other_503s() -> None:
    assert (
        _is_module_unavailable_503(
            _resp(503, b'{"detail": "Encryption not configured"}', "application/json")
        )
        is None
    )
    assert (
        _is_module_unavailable_503(_resp(503, b"<html>down</html>", "text/html"))
        is None
    )
    assert _is_module_unavailable_503(_resp(503, b"not json", "application/json")) is None
    assert _is_module_unavailable_503(_resp(503, b'["list"]', "application/json")) is None
    # A numeric-status body WITHOUT the module_unavailable code is not shape 2.
    assert (
        _is_module_unavailable_503(
            _resp(503, b'{"status": 503, "code": "other"}', "application/json")
        )
        is None
    )


def test_classifier_ignores_non_503() -> None:
    assert (
        _is_module_unavailable_503(
            _resp(404, b'{"status": "unavailable", "module": "x"}', "application/json")
        )
        is None
    )
