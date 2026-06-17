"""POST /journal-entries/{id}/reverse forwards reversal_date + override_reason.

Lets a 30-Jun accrual be reversed on 1-Jul from the GUI (the api now accepts
reversal_date on reverse). Without either field the request body stays empty
(reversal lands on the original entry's date — byte-identical old path).
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
_REV_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_API_BASE = settings.api_url.rstrip("/")

_MOCK_REVERSAL = {
    "id": _REV_ID,
    "company_id": "44444444-4444-4444-4444-444444444444",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "ref": "JE-000011",
    "entry_date": "2025-07-01",
    "status": "POSTED",
    "description": "Reversal of JE-000010",
    "reference": None,
    "posted_at": "2026-06-04T02:00:00Z",
    "posted_by": "web",
    "version": 1,
    "created_at": "2026-06-04T02:00:00Z",
    "updated_at": "2026-06-04T02:00:00Z",
    "archived_at": None,
    "reversal_of_id": _JE_ID,
    "lines": [],
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})


@pytest.mark.anyio
@respx.mock
async def test_reverse_with_date_forwards_body(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(
        f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/reverse"
    ).mock(return_value=Response(201, json=_MOCK_REVERSAL))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/reverse",
            data={
                "version": "2",
                "reversal_date": "2025-07-01",
                "override_reason": "year-end accrual reversal",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/journal-entries/{_REV_ID}"
    sent = _json.loads(route.calls.last.request.content)
    assert sent["reversal_date"] == "2025-07-01"
    assert sent["override_reason"] == "year-end accrual reversal"


@pytest.mark.anyio
@respx.mock
async def test_reverse_without_date_sends_no_body(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(
        f"{_API_BASE}/api/v1/journal_entries/{_JE_ID}/reverse"
    ).mock(return_value=Response(201, json=_MOCK_REVERSAL))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_JE_ID}/reverse", data={"version": "2"}
        )

    assert resp.status_code == 303
    assert route.calls.last.request.content in (b"", None)
