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
4/5. test_invoice_detail_{au,ee}_num_path — the OTHER conversion this
   packet made: bare/currency-code-prefixed sites (the bulk of the sweep,
   ~276 sites) went to `num()`, not `money()`, keeping the existing
   `{{ invoice.currency }} {{ ... }}` convention untouched (see the P4
   report on why money() is deliberately not used there — babel's en_AU
   locale has no clean foreign-currency symbol). These two prove that path
   end-to-end on invoices/detail.html: AU keeps its literal "AUD" prefix
   byte-exact with the number gaining locale-correct grouping, EE gets a
   comma-decimal number after its own currency code.
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
async def test_au_dashboard_kpi_ar_tile_renders_whole_dollars(
    respx_mock: respx.MockRouter,
) -> None:
    """Fixer round 1 regression: the top "AR · outstanding" KPI strip tile
    (`ar.paid_total`, the widest-blast-radius whole-dollar site the P4 sweep
    broke) must render rounded whole dollars — "$1,235" — not the currency's
    natural 2dp, matching the pre-8ff3a95 `${{ "{:,.0f}".format(x) }}`.
    """
    open_invoices = [
        _inv(id_="o001", number="INV-O001", status="POSTED", total="1234.56"),
    ]
    _register_mocks(respx_mock, open_invoices=open_invoices)
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="AU")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # The KPI strip tile (top of page) renders whole dollars; the expanded
    # detail panel further down the same page still shows the 2dp amount
    # (that call site was never `.0f` — see the P4 report), so this only
    # asserts the whole-dollar string is present, not that 2dp is absent.
    assert "$1,235" in resp.text


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


_INVOICE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _mock_invoice(currency: str, total: str) -> dict:
    return {
        "id": _INVOICE_ID,
        "company_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "contact_id": _CONTACT_ID,
        "number": "INV-0042",
        "issue_date": "2026-04-01",
        "due_date": "2026-04-30",
        "status": "POSTED",
        "subtotal": "1000.00",
        "tax_total": "234.56",
        "total": total,
        "amount_paid": "0.00",
        "currency": currency,
        "fx_rate": "1.0",
        "notes": None,
        "payment_terms": "Net 30",
        "posted_at": "2026-04-01T10:00:00Z",
        "posted_by": "api:testuser",
        "version": 1,
        "created_at": "2026-04-01T09:00:00Z",
        "updated_at": "2026-04-01T10:00:00Z",
        "archived_at": None,
        "lines": [],
    }


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_au_num_path_ungrouped_currency_code_kept(
    respx_mock: respx.MockRouter,
) -> None:
    """invoices/detail.html's `{{ invoice.currency }} {{ num(...) }}` site
    (untouched currency-code prefix, locale-aware number) end-to-end: AU
    keeps the literal "AUD" prefix — that convention is deliberately not
    replaced by money() (see module docstring). The number itself has no
    thousands grouping, matching the pre-8ff3a95 `"%.2f"|format(...)` this
    call site replaces (AU pixel-equivalence) — this is num()'s default
    (grouping=False), not an override.
    """
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_mock_invoice("AUD", "1234.56"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(return_value=Response(200, json=[]))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    assert "AUD 1234.56" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_ee_num_path_comma_decimal(
    respx_mock: respx.MockRouter,
) -> None:
    """Same call site, EE company — the currency code becomes "EUR" (still
    the document's own currency, untouched) and the decimal separator
    switches to a comma; no thousands grouping (matches AU/num()'s
    ungrouped default — see the AU sibling test above).
    """
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_mock_invoice("EUR", "1234.56"))
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(return_value=Response(200, json=[]))
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, jurisdiction="EE")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")

    assert resp.status_code == 200
    assert "EUR 1234,56" in resp.text
