"""Tests for the reconciliation web views — Lane D cycle 49.

Covers the full D/49 route map wired to B/42 API endpoints:

1.  test_reconciliation_accounts_requires_auth       — 303 -> /login without session
2.  test_reconciliation_accounts_renders             — accounts picker shows accounts table
3.  test_reconciliation_accounts_empty               — no accounts shows empty state
4.  test_reconciliation_accounts_api_error           — API error shows error banner
5.  test_reconciliation_lines_requires_auth          — 303 -> /login without session
6.  test_reconciliation_lines_renders                — lines page shows unmatched BSLs
7.  test_reconciliation_lines_empty                  — no lines shows empty state
8.  test_reconciliation_suggest_requires_auth        — 303 -> /login without session
9.  test_reconciliation_suggest_renders              — suggest page shows entry candidates
10. test_reconciliation_suggest_empty                — no suggestions shows empty state
11. test_reconciliation_suggest_api_error            — API error shows error banner
12. test_reconciliation_match_requires_auth          — POST /match without session -> 303
13. test_reconciliation_match_success                — POST /match 200 -> 303 to lines page
14. test_reconciliation_match_error                  — POST /match 422 -> 303 with flash error
15. test_reconciliation_unmatch_requires_auth        — POST /unmatch without session -> 303
16. test_reconciliation_unmatch_success              — POST /unmatch 200 -> 303 to lines page
17. test_reconciliation_auto_match_requires_auth     — POST /auto-match without session -> 303
18. test_reconciliation_auto_match_success           — POST /auto-match -> 303 with matched count
19. test_reconciliation_auto_match_api_error         — POST /auto-match 500 -> 303 with error flash
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
# Constants
# ---------------------------------------------------------------------------

_ACCOUNT_ID = "aaaaaaaa-1111-2222-3333-444444444444"
_BSL_ID = "bbbbbbbb-1111-2222-3333-444444444444"
_ENTRY_ID = "cccccccc-1111-2222-3333-444444444444"

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "code": "1010", "name": "Business Cheque"}

_MOCK_BSL = {
    "id": _BSL_ID,
    "account_id": _ACCOUNT_ID,
    "txn_date": "2026-04-01",
    "description": "OFFICE SUPPLIES PTY LTD",
    "amount": "-250.00",
    "reference": "DD-2026-04",
    "status": "UNMATCHED",
    "matched_entry_id": None,
    "matched_at": None,
    "matched_by": None,
}

_MOCK_ENTRY = {
    "id": _ENTRY_ID,
    "ref": "JE-042",
    "entry_date": "2026-03-30",
    "description": "Office supplies payment",
    "status": "POSTED",
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-xyz"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Accounts picker: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciliation_accounts_requires_auth() -> None:
    """GET /reconciliation without a session redirects to /login (303)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/reconciliation")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Accounts picker: renders account list
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_accounts_renders(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation renders a table row for each reconcilable account."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[_MOCK_ACCOUNT])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reconciliation")

    assert resp.status_code == 200
    assert "Business Cheque" in resp.text
    assert "1010" in resp.text
    assert "Reconcile" in resp.text
    assert _ACCOUNT_ID in resp.text


# ---------------------------------------------------------------------------
# 3. Accounts picker: empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_accounts_empty(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation with no accounts shows the empty state message."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reconciliation")

    assert resp.status_code == 200
    assert "No reconcilable accounts" in resp.text


# ---------------------------------------------------------------------------
# 4. Accounts picker: API error shows banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_accounts_api_error(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation with API 500 renders an error banner."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(500, json={"detail": "Internal server error"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reconciliation")

    assert resp.status_code == 200
    assert "could not be loaded" in resp.text


# ---------------------------------------------------------------------------
# 5. Lines page: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciliation_lines_requires_auth() -> None:
    """GET /reconciliation/{account_id}/lines without session redirects to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/reconciliation/{_ACCOUNT_ID}/lines")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 6. Lines page: renders unmatched BSLs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_lines_renders(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation/{account_id}/lines renders unmatched BSLs and Auto Match."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[_MOCK_ACCOUNT])
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        return_value=Response(200, json=[_MOCK_BSL])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/reconciliation/{_ACCOUNT_ID}/lines")

    assert resp.status_code == 200
    assert "OFFICE SUPPLIES PTY LTD" in resp.text
    assert "Auto Match All" in resp.text
    assert "Suggest" in resp.text
    assert "Business Cheque" in resp.text  # account name from accounts list


# ---------------------------------------------------------------------------
# 7. Lines page: empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_lines_empty(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation/{account_id}/lines with no unmatched BSLs shows empty state."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[_MOCK_ACCOUNT])
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/reconciliation/{_ACCOUNT_ID}/lines")

    assert resp.status_code == 200
    assert "No unmatched lines" in resp.text


# ---------------------------------------------------------------------------
# 8. Suggest page: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciliation_suggest_requires_auth() -> None:
    """GET /reconciliation/{bsl_id}/suggest without session redirects to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(f"/reconciliation/{_BSL_ID}/suggest")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 9. Suggest page: renders entry candidates
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_suggest_renders(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation/{bsl_id}/suggest renders journal entry candidates."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/suggest/{_BSL_ID}").mock(
        return_value=Response(200, json=[_MOCK_ENTRY])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            f"/reconciliation/{_BSL_ID}/suggest",
            params={"account_id": _ACCOUNT_ID},
        )

    assert resp.status_code == 200
    assert "JE-042" in resp.text
    assert "Office supplies payment" in resp.text
    assert "POSTED" in resp.text
    assert "Match" in resp.text
    # entry_id hidden input must be present for the match form
    assert _ENTRY_ID in resp.text


# ---------------------------------------------------------------------------
# 10. Suggest page: empty state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_suggest_empty(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation/{bsl_id}/suggest with no candidates shows empty message."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/suggest/{_BSL_ID}").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/reconciliation/{_BSL_ID}/suggest")

    assert resp.status_code == 200
    assert "No suggestions found" in resp.text


# ---------------------------------------------------------------------------
# 11. Suggest page: API error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_suggest_api_error(respx_mock: respx.MockRouter) -> None:
    """GET /reconciliation/{bsl_id}/suggest with API 404 renders error banner."""
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/suggest/{_BSL_ID}").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/reconciliation/{_BSL_ID}/suggest")

    assert resp.status_code == 200
    assert "could not be loaded" in resp.text


# ---------------------------------------------------------------------------
# 12. Match: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciliation_match_requires_auth() -> None:
    """POST /reconciliation/match without session redirects to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/reconciliation/match",
            data={"bsl_id": _BSL_ID, "entry_id": _ENTRY_ID, "account_id": _ACCOUNT_ID},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 13. Match: success -> 303 to lines page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_match_success(respx_mock: respx.MockRouter) -> None:
    """POST /reconciliation/match with API 200 -> 303 to account lines page."""
    respx_mock.post(f"{_API_BASE}/api/v1/reconciliation/match").mock(
        return_value=Response(200, json={"status": "matched"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/reconciliation/match",
            data={
                "bsl_id": _BSL_ID,
                "entry_id": _ENTRY_ID,
                "account_id": _ACCOUNT_ID,
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/reconciliation/{_ACCOUNT_ID}/lines"


# ---------------------------------------------------------------------------
# 14. Match: error -> 303 with flash error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_match_error(respx_mock: respx.MockRouter) -> None:
    """POST /reconciliation/match with API 422 -> 303 with error in flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/reconciliation/match").mock(
        return_value=Response(422, json={"detail": "BSL already matched"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/reconciliation/match",
            data={
                "bsl_id": _BSL_ID,
                "entry_id": _ENTRY_ID,
                "account_id": _ACCOUNT_ID,
            },
        )

    assert resp.status_code == 303
    # Session cookie carries the flash; redirect issued
    assert "location" in resp.headers


# ---------------------------------------------------------------------------
# 15. Unmatch: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciliation_unmatch_requires_auth() -> None:
    """POST /reconciliation/{bsl_id}/unmatch without session -> 303 to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/reconciliation/{_BSL_ID}/unmatch")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 16. Unmatch: success -> 303 to lines page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_unmatch_success(respx_mock: respx.MockRouter) -> None:
    """POST /reconciliation/{bsl_id}/unmatch with API 200 -> 303 to lines page."""
    respx_mock.post(f"{_API_BASE}/api/v1/reconciliation/unmatch/{_BSL_ID}").mock(
        return_value=Response(200, json={"status": "unmatched"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            f"/reconciliation/{_BSL_ID}/unmatch",
            data={"account_id": _ACCOUNT_ID},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/reconciliation/{_ACCOUNT_ID}/lines"


# ---------------------------------------------------------------------------
# 17. Auto-match: auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciliation_auto_match_requires_auth() -> None:
    """POST /reconciliation/{account_id}/auto-match without session -> 303 to /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/reconciliation/{_ACCOUNT_ID}/auto-match")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 18. Auto-match: success -> 303 with matched count in flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_auto_match_success(respx_mock: respx.MockRouter) -> None:
    """POST /reconciliation/{account_id}/auto-match with API 200 -> 303 with count flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/reconciliation/auto_match").mock(
        return_value=Response(200, json={"matched": 7})
    )
    # Also mock accounts + unmatched for the lines page after redirect
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[_MOCK_ACCOUNT])
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(f"/reconciliation/{_ACCOUNT_ID}/auto-match")

    assert resp.status_code == 200
    # Flash message with count must appear after the redirect
    assert "7" in resp.text
    assert "matched" in resp.text.lower()


# ---------------------------------------------------------------------------
# 19. Auto-match: API error -> 303 with error flash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reconciliation_auto_match_api_error(respx_mock: respx.MockRouter) -> None:
    """POST /reconciliation/{account_id}/auto-match with API 500 -> 303 with error flash."""
    respx_mock.post(f"{_API_BASE}/api/v1/reconciliation/auto_match").mock(
        return_value=Response(500, json={"detail": "server error"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/accounts").mock(
        return_value=Response(200, json=[_MOCK_ACCOUNT])
    )
    respx_mock.get(f"{_API_BASE}/api/v1/reconciliation/unmatched").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=True,
    ) as client:
        resp = await client.post(f"/reconciliation/{_ACCOUNT_ID}/auto-match")

    assert resp.status_code == 200
    assert "failed" in resp.text.lower() or "500" in resp.text
