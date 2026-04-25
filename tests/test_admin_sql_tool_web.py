"""Tests for the Admin SQL Tool web views — Lane D cycle 54.

1.  test_sql_tool_requires_auth             — GET /admin/sql-tool without session -> 303 /login
2.  test_sql_tool_renders_editor            — GET /admin/sql-tool returns 200 with textarea
3.  test_sql_tool_execute_requires_auth     — POST /admin/sql-tool/execute without session -> 303
4.  test_sql_tool_execute_success           — POST with SQL, API 200 -> results rendered inline
5.  test_sql_tool_execute_api_error         — POST with SQL, API 400 -> error shown inline (200)
6.  test_sql_tool_prefill_from_query_param  — GET /admin/sql-tool?q=SELECT... pre-fills textarea
7.  test_sql_tool_nav_link                  — GET /accounts includes Admin SQL nav link
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

_MOCK_SQL_RESULT_HTML = """
<div class="result-table">
  <table>
    <thead><tr><th>id</th><th>name</th></tr></thead>
    <tbody><tr><td>1</td><td>test row</td></tr></tbody>
  </table>
  <p class="row-count">1 row</p>
</div>
"""


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-sqltool"})


# ---------------------------------------------------------------------------
# 1. Auth gate — GET /admin/sql-tool
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sql_tool_requires_auth() -> None:
    """GET /admin/sql-tool without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/sql-tool")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Renders SQL editor
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sql_tool_renders_editor() -> None:
    """GET /admin/sql-tool returns 200 with SQL textarea."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/sql-tool")

    assert resp.status_code == 200
    assert "<textarea" in resp.text
    assert "sql" in resp.text.lower()
    assert "Execute" in resp.text


# ---------------------------------------------------------------------------
# 3. Auth gate — POST /admin/sql-tool/execute
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sql_tool_execute_requires_auth() -> None:
    """POST /admin/sql-tool/execute without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/sql-tool/execute",
            data={"sql": "SELECT 1"},
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 4. Execute success — results rendered inline
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_sql_tool_execute_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/sql-tool/execute with API 200 -> results shown inline (HTMX fragment)."""
    respx_mock.post(f"{_API_BASE}/admin/sql").mock(
        return_value=Response(
            200,
            content=_MOCK_SQL_RESULT_HTML.encode(),
            headers={"content-type": "text/html"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/sql-tool/execute",
            data={"sql": "SELECT id, name FROM companies LIMIT 1"},
        )

    assert resp.status_code == 200
    # The results template embeds proxy_html from the API.
    assert "result-table" in resp.text or "row" in resp.text.lower()


# ---------------------------------------------------------------------------
# 5. Execute API error — error shown inline (not a redirect)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_sql_tool_execute_api_error(respx_mock: respx.MockRouter) -> None:
    """POST /admin/sql-tool/execute with API 400 -> error rendered inline (status 200)."""
    respx_mock.post(f"{_API_BASE}/admin/sql").mock(
        return_value=Response(
            400,
            json={"detail": "Only SELECT statements are permitted"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/sql-tool/execute",
            data={"sql": "DROP TABLE companies"},
        )

    # HTMX expects 200 even on errors so the swap happens.
    assert resp.status_code == 200
    assert "error" in resp.text.lower() or "permitted" in resp.text.lower()


# ---------------------------------------------------------------------------
# 6. Pre-fill from query param
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sql_tool_prefill_from_query_param() -> None:
    """GET /admin/sql-tool?q=... pre-fills the SQL textarea."""
    sql = "SELECT * FROM companies LIMIT 5"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/sql-tool", params={"q": sql})

    assert resp.status_code == 200
    assert "SELECT" in resp.text
    assert "companies" in resp.text


# ---------------------------------------------------------------------------
# 7. SQL nav link in setup sub-row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_sql_tool_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /accounts shows Admin SQL nav link in the setup sub-row."""
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
    assert "/admin/sql-tool" in resp.text
    assert "SQL" in resp.text
