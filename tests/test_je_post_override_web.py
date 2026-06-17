"""POST /journal-entries/{id}/post forwards override_reason to the API.

Posting into a period-locked range needs an override reason (F-04). The web
post action must forward it in the JSON body; without one it must send no
body (byte-identical to the ordinary post path).
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

_JE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_API_BASE = settings.api_url.rstrip("/")

_MOCK_JE_POSTED = {
    "id": _JE_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000010",
    "entry_date": "2025-06-30",
    "status": "POSTED",
    "description": "Year-end accrual",
    "reference": None,
    "posted_at": "2026-06-04T01:00:00Z",
    "posted_by": "web",
    "version": 2,
    "created_at": "2026-06-04T00:00:00Z",
    "updated_at": "2026-06-04T01:00:00Z",
    "archived_at": None,
    "lines": [],
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


@pytest.mark.anyio
@respx.mock
async def test_je_post_with_override_forwards_reason(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(
        f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/post"
    ).mock(return_value=Response(200, json=_MOCK_JE_POSTED))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "1", "override_reason": "Year-end adjustment"},
        )

    assert resp.status_code == 303
    assert route.called
    sent = _json.loads(route.calls.last.request.content)
    assert sent.get("override_reason") == "Year-end adjustment"


@pytest.mark.anyio
@respx.mock
async def test_je_post_without_override_sends_no_body(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(
        f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/post"
    ).mock(return_value=Response(200, json=_MOCK_JE_POSTED))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/post",
            data={"version": "1"},
        )

    assert resp.status_code == 303
    assert route.called
    assert route.calls.last.request.content in (b"", None)
