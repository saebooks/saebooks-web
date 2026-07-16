"""Registry-driven sidebar nav (M2 app-lane step 9) + edition badge (9a).

Fixture catalogue/usage seeded through the real middleware path: the
catalogue via a mocked GET /api/v1/modules (process cache invalidated per
test), the usage snapshot via the session cookie.
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

_CATALOGUE = {
    "modules": [
        {
            "id": "bank_feeds",
            "label": "Bank Feeds",
            "kind": "flag",
            "group": "banking",
            "tier_membership": "business",
            "state": "enforced",
        },
        {
            "id": "asset_forecasts",
            "label": "Asset Forecasts",
            "kind": "flag",
            "group": "assets",
            "tier_membership": "pro",
            "state": "planned",
        },
        {
            "id": "secret_pro_module",
            "label": "Secret Pro Module",
            "kind": "flag",
            "group": "accounting",
            "tier_membership": "pro",
            "state": "enforced",
        },
        {
            "id": "future_integration",
            "label": "Future Integration",
            "kind": "flag",
            "group": "integrations",  # no matching nav section → catch-all
            "tier_membership": "business",
            "state": "planned",
        },
    ]
}

_USAGE = {
    "edition": "business",
    "effective_edition": "pro",
    "modules": {
        "bank_feeds": {"entitled": True, "health": "degraded"},
        "secret_pro_module": {"entitled": False, "health": "ok"},
    },
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-nav", "locale": "en", "module_usage": _USAGE}
)


@pytest.fixture(autouse=True)
def _fresh_catalogue_cache():
    module_registry.invalidate_catalogue_cache()
    yield
    module_registry.invalidate_catalogue_cache()


@pytest.fixture()
def _bank_feeds_href(monkeypatch: pytest.MonkeyPatch):
    """Entitled rows render only with a web-side href mapping."""
    monkeypatch.setattr(
        module_registry,
        "_MODULE_HREFS",
        {"bank_feeds": {"href": "/bank-feeds", "icon": "landmark"}},
    )


def _mock_page(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/modules").mock(
        return_value=Response(200, json=_CATALOGUE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={"items": []})
    )


async def _render_accounts() -> str:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.anyio
@respx.mock
async def test_entitled_module_renders_with_health_dot(
    respx_mock: respx.MockRouter, _bank_feeds_href
) -> None:
    _mock_page(respx_mock)
    html = await _render_accounts()
    # Entitled + href-mapped → link in the banking section with amber dot
    # (health=degraded).
    assert 'href="/bank-feeds"' in html
    assert "Bank Feeds" in html
    assert 'title="degraded"' in html


@pytest.mark.anyio
@respx.mock
async def test_planned_module_renders_coming_soon_unlinked(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_page(respx_mock)
    html = await _render_accounts()
    # Planned module in a mapped group (assets) → muted row, no link.
    assert "Asset Forecasts" in html
    assert 'href="/modules/asset_forecasts"' not in html
    assert "asset_forecasts" not in html.replace("Asset Forecasts", "")


@pytest.mark.anyio
@respx.mock
async def test_not_entitled_enforced_module_omitted(
    respx_mock: respx.MockRouter,
) -> None:
    # 404-not-403 no-advertise convention, preserved web-side.
    _mock_page(respx_mock)
    html = await _render_accounts()
    assert "Secret Pro Module" not in html


@pytest.mark.anyio
@respx.mock
async def test_unmapped_group_planned_module_in_coming_soon(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_page(respx_mock)
    html = await _render_accounts()
    assert 'data-section="coming-soon"' in html
    assert "Future Integration" in html


@pytest.mark.anyio
@respx.mock
async def test_entitled_module_without_href_not_rendered(
    respx_mock: respx.MockRouter,
) -> None:
    # No _MODULE_HREFS mapping (default empty) → no dead link is fabricated.
    _mock_page(respx_mock)
    html = await _render_accounts()
    assert 'href="/bank-feeds"' not in html
    assert 'href="/modules/bank_feeds"' not in html


@pytest.mark.anyio
@respx.mock
async def test_edition_badge_uses_effective_edition(
    respx_mock: respx.MockRouter,
) -> None:
    """Step 9a finding-5 regression: promo user's badge shows the usage
    payload's effective_edition (pro), not the process env var
    (community default) — badge and registry nav agree."""
    _mock_page(respx_mock)
    html = await _render_accounts()
    assert ">pro</span>" in html
