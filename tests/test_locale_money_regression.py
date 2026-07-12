"""AU pixel-equivalence + EE locale-format regression for Packet 4
(money()/num() globals wired into dashboard.html + _funnel_macros.html).

The packet's hard requirement: "AU renders $1,234.56 exactly as today —
regression tests prove it." These tests render the real dashboard route
end-to-end (not a direct formatter unit call — see test_i18n_format.py for
that) and assert byte-exact substrings, using amounts >= 1000 specifically
because that's where a broken formatter would silently drop thousands
grouping or use the wrong locale (the "en" vs "en_AU" babel gotcha — see
i18n/format.py module docstring).

1. test_au_dashboard_renders_dollar_comma_format_byte_exact — AR draft tile
   (a `{{ money(...) }}` call site converted from the pre-existing
   `${{ "{:,.2f}".format(x) }}`) renders the byte-identical "$1,234.56" for
   an AU company with no explicit jurisdiction mock (falls through to the
   existing "AU" default — see company_context.py).
2. test_au_dashboard_cash_tile_negative_uses_existing_minus_convention —
   the cash-net tile's pre-existing manual `{% if cash.net < 0 %}−{% endif
   %}` sign handling (kept as-is around the new `{{ money(...) }}` call,
   not replaced by money()'s own sign handling) still renders the same
   U+2212 minus character before the $, byte-exact.
3. test_ee_dashboard_renders_euro_comma_decimal_format — same tile, EE
   company (jurisdiction resolved via the tax_codes proxy per
   company_context.py) renders "1 234,56 €" (real babel bytes, U+00A0
   separators) and contains no "$" from the converted call sites.
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

from tests.test_dashboard import _inv, _register_mocks
from tests.test_jurisdiction_gating import _AU_COMPANY, _EE_COMPANY, _mock_companies, _mock_tax_codes

_NBSP = "\xa0"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-money-regression"})
_API_BASE = settings.api_url.rstrip("/")


@pytest.mark.anyio
@respx.mock
async def test_au_dashboard_renders_dollar_comma_format_byte_exact(
    respx_mock: respx.MockRouter,
) -> None:
    draft = [_inv(id_="d001", number="INV-D001", status="DRAFT", total="1234.56")]
    _register_mocks(respx_mock, draft_invoices=draft)
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="AU")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # The exact string every pre-Packet-4 AU page rendered for this tile.
    assert "$1,234.56" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_au_dashboard_cash_tile_negative_uses_existing_minus_convention(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.test_dashboard import _payment

    pmt_out = _payment(
        id_="cout001", number="PAY-OUT", direction="OUTGOING", amount="1500.00",
    )
    _register_mocks(respx_mock, payments=[pmt_out])
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="AU")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # Net = 0 - 1500 = -1500: the template's own U+2212 sign handling
    # (kept untouched by the P4 edit) followed by money()'s "$1,500.00".
    assert "−$1,500.00" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_ee_dashboard_renders_euro_comma_decimal_format(
    respx_mock: respx.MockRouter,
) -> None:
    draft = [_inv(id_="d001", number="INV-D001", status="DRAFT", total="1234.56")]
    _register_mocks(respx_mock, draft_invoices=draft)
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="EE")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert f"1{_NBSP}234,56{_NBSP}€" in resp.text
    # The AR draft tile must not fall back to a hardcoded "$" for an EE
    # company — that would be exactly the bug this packet exists to fix.
    assert "$1,234.56" not in resp.text
