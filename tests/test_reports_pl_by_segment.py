"""Tests for the P&L by Segment HTML view — Lane D cycle 41.

Tests:
1. test_pl_by_segment_get_200      — full-page GET 200, segments rendered with net profit
2. test_pl_by_segment_htmx_partial — HX-Request returns fragment (no <html>)
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
# Helpers
# ---------------------------------------------------------------------------


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")

# ---------------------------------------------------------------------------
# Mock API response fixture
# ---------------------------------------------------------------------------

_SEG_REPORT = {
    "from_date": "2026-04-01",
    "to_date": "2026-04-30",
    "segment_type": "project",
    "segments": [
        {
            "segment_id": "proj-001",
            "segment_label": "Project Alpha",
            "sections": [
                {
                    "account_type": "INCOME",
                    "lines": [
                        {
                            "account_id": "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                            "code": "4000",
                            "name": "Consulting Revenue",
                            "amount": 5000.0,
                        }
                    ],
                    "total": 5000.0,
                },
                {
                    "account_type": "EXPENSE",
                    "lines": [
                        {
                            "account_id": "bbbb0002-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                            "code": "6000",
                            "name": "Subcontractors",
                            "amount": 1800.0,
                        }
                    ],
                    "total": 1800.0,
                },
            ],
            "net_profit": 3200.0,
        },
        {
            "segment_id": "proj-002",
            "segment_label": "Project Beta",
            "sections": [
                {
                    "account_type": "INCOME",
                    "lines": [
                        {
                            "account_id": "cccc0003-cccc-cccc-cccc-cccccccccccc",
                            "code": "4000",
                            "name": "Consulting Revenue",
                            "amount": 2000.0,
                        }
                    ],
                    "total": 2000.0,
                },
                {
                    "account_type": "EXPENSE",
                    "lines": [
                        {
                            "account_id": "dddd0004-dddd-dddd-dddd-dddddddddddd",
                            "code": "6200",
                            "name": "Materials",
                            "amount": 3500.0,
                        }
                    ],
                    "total": 3500.0,
                },
            ],
            "net_profit": -1500.0,
        },
    ],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_pl_by_segment_get_200(respx_mock: respx.MockRouter) -> None:
    """GET /reports/pl-by-segment returns 200 full page with segment labels and net profit."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/pl_by_segment.*$").mock(
        return_value=Response(200, json=_SEG_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/reports/pl-by-segment")

    assert resp.status_code == 200
    assert "<html" in resp.text
    assert "Segment" in resp.text
    # Segment labels
    assert "Project Alpha" in resp.text
    assert "Project Beta" in resp.text
    # Account lines
    assert "Consulting Revenue" in resp.text
    assert "Subcontractors" in resp.text
    assert "5000.00" in resp.text
    assert "1800.00" in resp.text
    # Net profit values
    assert "3200.00" in resp.text
    assert "-1500.00" in resp.text
    # Colour coding — green for positive profit, red for negative
    assert "green" in resp.text
    assert "red" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_pl_by_segment_htmx_partial(respx_mock: respx.MockRouter) -> None:
    """GET /reports/pl-by-segment with HX-Request returns fragment, no <html>."""
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/reports/pl_by_segment.*$").mock(
        return_value=Response(200, json=_SEG_REPORT)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(
            "/reports/pl-by-segment",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "<html" not in resp.text
    # Fragment wrapper present
    assert "report-content" in resp.text
    # Data still present
    assert "Project Alpha" in resp.text
    assert "3,200.00" in resp.text
