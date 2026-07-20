"""Cashbook nav "Full accounting" doorway.

Cashbook mode must carry exactly one link into the full double-entry
suite, framed as ownership (you already have the ledger) rather than a
paywall/upsell. It must be present in the cashbook sidebar and absent
from the full (non-cashbook) sidebar.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web import module_registry
from saebooks_web.config import settings
from saebooks_web.main import app

_API_BASE = settings.api_url.rstrip("/")

_COMPANY_ID = "aaaa0001-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-doorway", "locale": "en"}
)


@pytest.fixture(autouse=True)
def _fresh_catalogue_cache():
    module_registry.invalidate_catalogue_cache()
    yield
    module_registry.invalidate_catalogue_cache()


def _mock_common(respx_mock: respx.MockRouter, bookkeeping_mode: str) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json={"modules": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "id": _COMPANY_ID,
                        "name": "Test Co",
                        "trading_name": "Test Co",
                        "created_at": "2026-01-01T00:00:00Z",
                        "bookkeeping_mode": bookkeeping_mode,
                    }
                ]
            },
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": []})
    )


async def _get(path: str) -> str:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(path)
    assert resp.status_code == 200
    return resp.text


@pytest.mark.anyio
@respx.mock
async def test_cashbook_nav_shows_full_accounting_doorway(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_common(respx_mock, "cashbook")
    html = await _get("/cashbook/about")
    assert "Full accounting →" in html
    assert 'href="/cashbook/upgrade"' in html


@pytest.mark.anyio
@respx.mock
async def test_full_mode_nav_has_no_doorway(respx_mock: respx.MockRouter) -> None:
    _mock_common(respx_mock, "standard")
    html = await _get("/accounts")
    assert "Full accounting →" not in html
    assert 'href="/cashbook/upgrade"' not in html


@pytest.mark.anyio
@respx.mock
async def test_cashbook_nav_carries_exactly_one_doorway_item(
    respx_mock: respx.MockRouter,
) -> None:
    """Exactly one nav item points at the upgrade explainer — not a second,
    differently-worded upsell link duplicating it."""
    _mock_common(respx_mock, "cashbook")
    html = await _get("/cashbook/about")
    assert html.count('href="/cashbook/upgrade"') == 1


@pytest.mark.anyio
@respx.mock
async def test_upgrade_explainer_reads_ownership_not_paywall(
    respx_mock: respx.MockRouter,
) -> None:
    """The explainer page itself renders and carries the ownership framing
    — never the old "unlock"/paywall wording it replaced."""
    _mock_common(respx_mock, "cashbook")
    html = await _get("/cashbook/upgrade")
    assert "You already own a complete double-entry ledger" in html
    assert "already journal entries" in html
    assert "Unlock" not in html
    assert "Upgrade to Full" not in html
