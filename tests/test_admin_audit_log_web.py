"""Tests for the Admin Audit Log web views — Lane D cycle 54.

1.  test_audit_log_requires_auth          — GET /admin/audit without session -> 303 /login
2.  test_audit_log_renders                — GET /admin/audit returns 200 with audit log page
3.  test_audit_log_api_error              — GET /admin/audit with API 500 shows error banner
4.  test_audit_log_filter_params          — GET /admin/audit?entity_type=X echoes filter in form
5.  test_audit_log_pagination_next        — GET /admin/audit with has_next shows next link
6.  test_audit_log_nav_link               — GET /contacts shows Audit nav link
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

_MOCK_AUDIT_HTML = """
<div class="audit-snapshots">
  <table>
    <thead><tr><th>Timestamp</th><th>Action</th><th>Entity</th></tr></thead>
    <tbody>
      <tr>
        <td>2026-04-25T10:00:00</td>
        <td>CREATE</td>
        <td>journal_entries / abcdef12</td>
      </tr>
    </tbody>
  </table>
</div>
"""


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-audit"})


# ---------------------------------------------------------------------------
# 1. Auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_audit_log_requires_auth() -> None:
    """GET /admin/audit without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Renders audit log page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_audit_log_renders(respx_mock: respx.MockRouter) -> None:
    """GET /admin/audit returns 200 with audit log heading."""
    respx_mock.get(f"{_API_BASE}/admin/audit").mock(
        return_value=Response(
            200,
            content=_MOCK_AUDIT_HTML.encode(),
            headers={"content-type": "text/html"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 200
    assert "Audit Log" in resp.text
    assert "Filter" in resp.text


# ---------------------------------------------------------------------------
# 3. API error shows error banner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_audit_log_api_error(respx_mock: respx.MockRouter) -> None:
    """GET /admin/audit with API 500 shows error banner."""
    respx_mock.get(f"{_API_BASE}/admin/audit").mock(
        return_value=Response(500, json={"detail": "Internal server error"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 200
    assert "API error" in resp.text or "500" in resp.text


# ---------------------------------------------------------------------------
# 4. Filter params echoed back in form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_audit_log_filter_params(respx_mock: respx.MockRouter) -> None:
    """GET /admin/audit?entity_type=journal_entries echoes filter in form."""
    respx_mock.get(f"{_API_BASE}/admin/audit").mock(
        return_value=Response(
            200,
            content=_MOCK_AUDIT_HTML.encode(),
            headers={"content-type": "text/html"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/audit", params={"entity_type": "journal_entries"})

    assert resp.status_code == 200
    # Filter form should contain the submitted entity_type value.
    assert "journal_entries" in resp.text


# ---------------------------------------------------------------------------
# 5. Pagination next link when has_next (JSON API)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_audit_log_pagination_next(respx_mock: respx.MockRouter) -> None:
    """GET /admin/audit with JSON response has_next=True shows next-page link."""
    respx_mock.get(f"{_API_BASE}/admin/audit").mock(
        return_value=Response(
            200,
            json={
                "snapshots": [
                    {
                        "id": "aaaaaaaa-0000-0000-0000-000000000001",
                        "action": "CREATE",
                        "table_name": "journal_entries",
                        "row_id": "bbbbbbb1",
                        "performed_by": "admin",
                        "performed_at": "2026-04-25T10:00:00",
                    }
                ],
                "has_next": True,
                "tables": ["journal_entries"],
            },
            headers={"content-type": "application/json"},
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/audit")

    assert resp.status_code == 200
    # When has_next, pagination renders a "Next" link.
    assert "Next" in resp.text or "page=2" in resp.text


# ---------------------------------------------------------------------------
# 6. Audit nav link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_audit_log_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /contacts shows Audit nav link in setup sub-row."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/contacts")

    assert resp.status_code == 200
    assert "/admin/audit" in resp.text
    assert "Audit" in resp.text
