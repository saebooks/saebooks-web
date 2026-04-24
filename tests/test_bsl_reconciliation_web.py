"""Tests for bank statement line match/unmatch reconciliation UI — Lane D cycle 36.

Four tests:
1. test_match_bsl_renders_form          — GET detail of UNMATCHED line shows match form
2. test_match_bsl_success_redirects     — POST /match -> 303 on API 200
3. test_unmatch_bsl_success_redirects   — POST /unmatch -> 303 on API 200
4. test_matched_line_shows_unmatch_button — detail of MATCHED line shows Unmatch button
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
# Fixtures / helpers
# ---------------------------------------------------------------------------

_LINE_ID = "aaaaaaaa-bbbb-cccc-dddd-000000000099"
_PAYMENT_ID = "11111111-2222-3333-4444-555555555555"

_MOCK_LINE_UNMATCHED = {
    "id": _LINE_ID,
    "company_id": "cccccccc-cccc-cccc-cccc-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "account_id": "dddddddd-dddd-dddd-dddd-000000000001",
    "txn_date": "2026-04-20",
    "description": "DIRECT DEBIT UTILITIES",
    "amount": "-350.00",
    "balance": "8500.00",
    "reference": "DD-2026-04",
    "status": "UNMATCHED",
    "matched_to_type": None,
    "matched_to_id": None,
    "matched_at": None,
    "matched_entry_id": None,
    "matched_by": None,
    "contact_id": None,
    "bank_rule_id": None,
    "bank_feed_account_id": None,
    "external_id": None,
    "version": 1,
    "created_at": "2026-04-20T09:00:00Z",
    "archived_at": None,
}

_MOCK_LINE_MATCHED = {
    **_MOCK_LINE_UNMATCHED,
    "status": "MATCHED",
    "matched_to_type": "PAYMENT",
    "matched_to_id": _PAYMENT_ID,
    "matched_at": "2026-04-21T10:30:00Z",
    "version": 2,
}


def _make_session_cookie(data: dict) -> str:
    """Encode a session dict the same way Starlette's SessionMiddleware does."""
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Unmatched line detail shows the match form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_match_bsl_renders_form(respx_mock: respx.MockRouter) -> None:
    """GET /bank-statement-lines/{id} for an UNMATCHED line renders the match form."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines/{_LINE_ID}").mock(
        return_value=Response(200, json=_MOCK_LINE_UNMATCHED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bank-statement-lines/{_LINE_ID}")

    assert resp.status_code == 200
    # Match form inputs must be present.
    assert 'name="matched_to_type"' in resp.text
    assert 'name="matched_to_id"' in resp.text
    # Submit button.
    assert "Match" in resp.text
    # The unmatch form action must NOT appear for an unmatched line.
    assert f"/bank-statement-lines/{_LINE_ID}/unmatch" not in resp.text


# ---------------------------------------------------------------------------
# 2. POST /match -> API 200 -> 303 redirect to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_match_bsl_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /bank-statement-lines/{id}/match; API 200 -> 303 redirect to detail."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/bank_statement_lines/{_LINE_ID}/match"
    ).mock(return_value=Response(200, json=_MOCK_LINE_MATCHED))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/bank-statement-lines/{_LINE_ID}/match",
            data={
                "matched_to_type": "PAYMENT",
                "matched_to_id": _PAYMENT_ID,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bank-statement-lines/{_LINE_ID}"


# ---------------------------------------------------------------------------
# 3. POST /unmatch -> API 200 -> 303 redirect to detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_unmatch_bsl_success_redirects(respx_mock: respx.MockRouter) -> None:
    """POST /bank-statement-lines/{id}/unmatch; API 200 -> 303 redirect to detail."""
    respx_mock.post(
        f"{_API_BASE}/api/v1/bank_statement_lines/{_LINE_ID}/unmatch"
    ).mock(return_value=Response(200, json=_MOCK_LINE_UNMATCHED))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/bank-statement-lines/{_LINE_ID}/unmatch")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bank-statement-lines/{_LINE_ID}"


# ---------------------------------------------------------------------------
# 4. Matched line detail shows the Unmatch button
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_matched_line_shows_unmatch_button(respx_mock: respx.MockRouter) -> None:
    """GET /bank-statement-lines/{id} for a MATCHED line shows Unmatch button and match info."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank_statement_lines/{_LINE_ID}").mock(
        return_value=Response(200, json=_MOCK_LINE_MATCHED)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/bank-statement-lines/{_LINE_ID}")

    assert resp.status_code == 200
    # Unmatch button (form action) must be present.
    assert f"/bank-statement-lines/{_LINE_ID}/unmatch" in resp.text
    # Match details rendered.
    assert "PAYMENT" in resp.text
    assert _PAYMENT_ID in resp.text
    # The match form inputs must NOT appear for an already-matched line.
    assert 'name="matched_to_id"' not in resp.text
