"""Web-flow tests for eID login/link: gating, HTMX flow, session mint, CSRF.

The SK transport is replaced with a fake provider; the engine
(oauth-handoff, /auth/me) is mocked with respx. Gating tests prove the
free tier never sees the surface and the routes refuse like the other
gated features (404).
"""
from __future__ import annotations

import json as _json
from base64 import b64decode as _b64decode
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web import eid_sso
from saebooks_web.config import settings
from saebooks_web.eid_providers import (
    EidAssertion,
    EidStart,
    EidTimeout,
    EidUserRefused,
)
from saebooks_web.main import app

CODE = "40504040001"

_API = settings.api_url.rstrip("/")
_HANDOFF_URL = f"{_API}/api/v1/auth/oauth-handoff"
_ME_URL = f"{_API}/api/v1/auth/me"

_ASSERTION = EidAssertion(
    personal_code=CODE,
    country="EE",
    given_name="OK",
    surname="TEST",
    document_number=f"PNOEE-{CODE}-FIX-Q",
    provider="smart-id",
)

_ME = {
    "id": "11111111-1111-1111-1111-111111111111",
    "username": "mari",
    "email": "mari@example.ee",
    "name": "Mari Maasikas",
    "role": "bookkeeper",
}


class _FakeProvider:
    """Scriptable provider: first N polls pending, then result/error."""

    key = "smart-id"

    def __init__(self, outcome=None, pending_polls: int = 0):
        self.outcome = outcome
        self.pending_polls = pending_polls

    async def start_authentication(self, personal_code, *, phone_number=None, language="et"):
        return EidStart(
            provider=self.key,
            verification_code="4711",
            state={"provider": self.key, "session_id": "s-1", "personal_code": personal_code},
        )

    async def check_session(self, state):
        if self.pending_polls > 0:
            self.pending_polls -= 1
            return None
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SAEBOOKS_EDITION", "business")
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    monkeypatch.setenv("SAEBOOKS_EID_LINK_STORE", str(tmp_path / "links.json"))
    monkeypatch.setenv("SAEBOOKS_OAUTH_HANDOFF_SECRET", "test-handoff-secret")


@pytest.fixture
def fake_provider(monkeypatch):
    fake = _FakeProvider(outcome=_ASSERTION)
    monkeypatch.setattr(eid_sso, "get_provider", lambda key: fake)
    return fake


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


def _decode_session_cookie(cookie_value: str) -> dict:
    signer = _TimestampSigner(settings.secret_key)
    payload = signer.unsign(cookie_value.encode(), max_age=None)
    return _json.loads(_b64decode(payload))


# ---------------------------------------------------------------------------
# Gating — free tier / wrong brand never sees the surface
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_flag_off_hides_and_refuses(monkeypatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "community")
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur")
    async with _client() as client:
        login = await client.get("/login")
        assert "eid-login-link" not in login.text
        assert (await client.get("/auth/eid/login")).status_code == 404
        assert (
            await client.post(
                "/auth/eid/start",
                data={"provider": "smart-id", "personal_code": CODE, "mode": "login"},
            )
        ).status_code == 404
        assert (await client.post("/auth/eid/poll")).status_code == 404
        assert (await client.get("/settings/eid")).status_code == 404
        assert (await client.post("/settings/eid/unlink")).status_code == 404


@pytest.mark.anyio
async def test_paid_tier_but_non_ee_brand_hides(monkeypatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "pro")
    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    monkeypatch.delenv("SAEBOOKS_EID_UI", raising=False)
    async with _client() as client:
        assert (await client.get("/auth/eid/login")).status_code == 404
    # …unless the deployment opts in explicitly.
    monkeypatch.setenv("SAEBOOKS_EID_UI", "1")
    async with _client() as client:
        assert (await client.get("/auth/eid/login")).status_code == 200


@pytest.mark.anyio
async def test_login_page_shows_eid_when_enabled(enabled) -> None:
    async with _client() as client:
        login = await client.get("/login")
        assert 'data-testid="eid-login-link"' in login.text
        page = await client.get("/auth/eid/login")
        assert page.status_code == 200
        assert "Smart-ID" in page.text and "Mobiil-ID" in page.text
        # Start forms carry the CSRF hidden input.
        assert page.text.count('name="csrf_token"') >= 2


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_login_flow_linked_user_gets_session(
    enabled, fake_provider, respx_mock
) -> None:
    from saebooks_web import eid_links

    eid_links.link("mari@example.ee", CODE, "smart-id")
    respx_mock.post(_HANDOFF_URL).mock(
        return_value=Response(200, json={"access_token": "jwt-token-1"})
    )
    respx_mock.get(_ME_URL).mock(return_value=Response(200, json=_ME))

    fake_provider.pending_polls = 1
    async with _client() as client:
        start = await client.post(
            "/auth/eid/start",
            data={"provider": "smart-id", "personal_code": CODE, "mode": "login"},
        )
        assert start.status_code == 200
        assert "4711" in start.text  # verification code shown
        assert 'hx-post="/auth/eid/poll"' in start.text

        pending = await client.post("/auth/eid/poll")
        assert pending.status_code == 200
        assert "4711" in pending.text  # still waiting, code re-rendered

        done = await client.post("/auth/eid/poll")
        assert done.status_code == 200
        assert done.headers.get("HX-Redirect") == "/"

        session = _decode_session_cookie(client.cookies["saebooks_web_session"])
        assert session["api_token"] == "jwt-token-1"
        assert session["username"] == "Mari Maasikas"
        assert "eid_auth" not in session  # flow state scrubbed

    # The handoff carried the eID identity, not a password.
    handoff_body = _json.loads(respx_mock.calls[0].request.content)
    assert handoff_body["provider"] == "eid"
    assert handoff_body["provider_user_id"] == f"PNOEE-{CODE}"
    assert handoff_body["email"] == "mari@example.ee"


@pytest.mark.anyio
async def test_login_flow_unlinked_user_refused(enabled, fake_provider) -> None:
    async with _client() as client:
        await client.post(
            "/auth/eid/start",
            data={"provider": "smart-id", "personal_code": CODE, "mode": "login"},
        )
        done = await client.post("/auth/eid/poll")
        assert done.status_code == 200
        assert 'data-testid="eid-error"' in done.text
        assert "no account is linked" in done.text
        assert "HX-Redirect" not in done.headers
        session = _decode_session_cookie(client.cookies["saebooks_web_session"])
        assert "api_token" not in session


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "needle"),
    [
        (EidUserRefused(), "declined"),
        (EidTimeout(), "expired"),
    ],
)
async def test_login_flow_terminal_errors(enabled, fake_provider, outcome, needle) -> None:
    fake_provider.outcome = outcome
    async with _client() as client:
        await client.post(
            "/auth/eid/start",
            data={"provider": "smart-id", "personal_code": CODE, "mode": "login"},
        )
        done = await client.post("/auth/eid/poll")
        assert 'data-testid="eid-error"' in done.text
        assert needle in done.text


@pytest.mark.anyio
async def test_start_rejects_invalid_personal_code(enabled, fake_provider) -> None:
    async with _client() as client:
        resp = await client.post(
            "/auth/eid/start",
            data={"provider": "smart-id", "personal_code": "not-a-code", "mode": "login"},
        )
        assert resp.status_code == 200
        assert 'data-testid="eid-error"' in resp.text


@pytest.mark.anyio
async def test_poll_without_flow_state_errors_cleanly(enabled) -> None:
    async with _client() as client:
        resp = await client.post("/auth/eid/poll")
        assert resp.status_code == 200
        assert 'data-testid="eid-error"' in resp.text


# ---------------------------------------------------------------------------
# Settings — link management
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_settings_link_and_unlink(enabled, fake_provider, respx_mock) -> None:
    from saebooks_web import eid_links

    respx_mock.get(_ME_URL).mock(return_value=Response(200, json=_ME))
    cookie = _session_cookie({"api_token": "jwt-token-1", "username": "mari"})

    async with _client() as client:
        client.cookies.set("saebooks_web_session", cookie)
        page = await client.get("/settings/eid")
        assert page.status_code == 200
        assert 'data-testid="eid-not-linked"' in page.text

        start = await client.post(
            "/auth/eid/start",
            data={"provider": "smart-id", "personal_code": CODE, "mode": "link"},
        )
        assert start.status_code == 200 and "4711" in start.text

        done = await client.post("/auth/eid/poll")
        assert done.status_code == 200
        assert 'data-testid="eid-link-success"' in done.text
        assert eid_links.find_link(CODE)["email"] == "mari@example.ee"

        linked_page = await client.get("/settings/eid")
        assert 'data-testid="eid-linked"' in linked_page.text
        assert "405••••••01" in linked_page.text

        unlink = await client.post("/settings/eid/unlink")
        assert unlink.status_code == 303
        assert eid_links.find_link(CODE) is None


@pytest.mark.anyio
async def test_settings_requires_authentication(enabled) -> None:
    async with _client() as client:
        resp = await client.get("/settings/eid")
        assert resp.status_code == 303  # redirect to /login
        start = await client.post(
            "/auth/eid/start",
            data={"provider": "smart-id", "personal_code": CODE, "mode": "link"},
        )
        assert start.status_code == 401
        assert (await client.post("/settings/eid/unlink")).status_code == 401


@pytest.mark.anyio
async def test_csrf_enforced_on_authenticated_eid_posts(enabled, fake_provider) -> None:
    """A logged-in session POSTing with a WRONG token must be rejected by
    the CSRF middleware (Layer 3)."""
    cookie = _session_cookie({"api_token": "jwt-token-1"})
    async with _client() as client:
        client.cookies.set("saebooks_web_session", cookie)
        resp = await client.post(
            "/auth/eid/start",
            data={
                "provider": "smart-id",
                "personal_code": CODE,
                "mode": "link",
                "csrf_token": "wrong-token-000000000000000000000000000000",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "csrf_token_mismatch"
