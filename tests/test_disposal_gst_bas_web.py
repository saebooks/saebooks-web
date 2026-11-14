"""Disposal GST and the "settle only after lodged" setting — web side (2026-11-14).

Engine changes these screens follow:

* An asset disposal now posts the GST on the sale and the gain or loss on
  the price excluding GST, exactly as the disposal preview shows. The
  dispose form offers "GST on the sale" (GST-registered businesses only)
  and sends ``gst_applies``; the preview takes the same choice. The old
  caveats ("raise an invoice for the sale and enter 0 here", "does not
  post ... the GST or the gain or loss") are gone.
* Company setting ``bas_settle_requires_lodged``: a checkbox in the GST
  section of company settings; the settle screen shows the engine's
  "not marked lodged yet" message with a link to the period on /bas.

Engine calls are mocked with respx.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app
from saebooks_web.routes.settings import _build_company_payload, _prefill_form

_API = settings.api_url.rstrip("/")
_ASSET_ID = "fa000000-0000-0000-0000-0000000000d1"
_COMPANY_ID = "c0000000-0000-0000-0000-0000000000d1"
_BANK = "aaaa0000-0000-0000-0000-0000000000d1"


def _cookie() -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": "test-token-dispgst"}).encode())
    return signer.sign(payload).decode()


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _cookie()},
        follow_redirects=False,
    )


_ASSET = {
    "id": _ASSET_ID, "company_id": _COMPANY_ID, "code": "AST-000009", "name": "Compressor",
    "description": None, "status": "active", "depreciation_model_id": "asset_5_year_linear",
    "depreciation_model": None, "tax_model_id": None, "cost_account_id": _BANK,
    "accum_dep_account_id": _BANK, "dep_expense_account_id": _BANK,
    "purchase_date": "2026-01-02", "in_service_date": None, "cost": "1000.00",
    "residual_value": "0.00", "last_depreciation_posted_through": None,
    "disposal_date": None, "disposal_proceeds": None, "disposal_journal_id": None,
    "version": 3, "created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
    "archived_at": None,
}


def _engine(registered: bool):  # type: ignore[no-untyped-def]
    """One respx side effect for every engine GET the detail page makes."""

    def handler(request) -> Response:  # type: ignore[no-untyped-def]
        path = request.url.path
        if path == "/api/v1/companies":
            return Response(200, json={"items": [{
                "id": _COMPANY_ID, "name": "Test Co", "created_at": "2026-01-01T00:00:00Z",
                "bookkeeping_mode": "full"}]})
        if path == f"/api/v1/companies/{_COMPANY_ID}":
            return Response(200, json={"id": _COMPANY_ID, "tax_registered": registered})
        if path == f"/api/v1/fixed_assets/{_ASSET_ID}":
            return Response(200, json=_ASSET)
        if path == "/api/v1/tax_codes":
            return Response(200, json={"items": [{"code": "GST", "jurisdiction": "AU"}]})
        return Response(200, json={"items": []})

    return handler


# ---------------------------------------------------------------------------
# Disposal
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_dispose_form_offers_gst_choice_when_registered(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/.*$").mock(side_effect=_engine(True))
    async with _client() as c:
        r = await c.get(f"/fixed-assets/{_ASSET_ID}")
    assert r.status_code == 200, r.text
    html = r.text
    assert 'name="gst_applies"' in html
    assert '<option value="true" selected>' in html  # default = registered
    assert "#disposal_date, #proceeds, #gst_applies" in html  # preview follows the choice
    assert "raise an invoice for the sale" not in html


@pytest.mark.anyio
@respx.mock
async def test_dispose_form_has_no_gst_choice_when_not_registered(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/.*$").mock(side_effect=_engine(False))
    async with _client() as c:
        r = await c.get(f"/fixed-assets/{_ASSET_ID}")
    assert r.status_code == 200, r.text
    assert 'name="gst_applies"' not in r.text
    assert "raise an invoice for the sale" not in r.text


@pytest.mark.anyio
@respx.mock
async def test_dispose_sends_the_gst_choice(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_API}/api/v1/fixed_assets/{_ASSET_ID}/dispose").mock(
        return_value=Response(200, json={"id": _ASSET_ID, "status": "disposed"})
    )
    base = {"version": "3", "disposal_date": "2026-11-01", "proceeds": "1650",
            "proceeds_account_id": _BANK}
    async with _client() as c:
        for choice, expected in (("true", True), ("false", False)):
            r = await c.post(f"/fixed-assets/{_ASSET_ID}/dispose",
                             data={**base, "gst_applies": choice})
            assert r.status_code == 303
            assert _json.loads(route.calls.last.request.content)["gst_applies"] is expected
        # Not on the form (not registered): left to the engine's default.
        r = await c.post(f"/fixed-assets/{_ASSET_ID}/dispose", data=base)
        assert r.status_code == 303
        assert "gst_applies" not in _json.loads(route.calls.last.request.content)


@pytest.mark.anyio
@respx.mock
async def test_preview_follows_the_gst_choice_and_says_it_posts(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(f"{_API}/api/v1/fixed_assets/{_ASSET_ID}/disposal-preview").mock(
        return_value=Response(200, json={
            "cost": "1000.00", "depreciation_to_catch_up": "0.00",
            "accumulated_depreciation": "0.00", "book_value": "1000.00",
            "sale_price": "1650.00", "sale_price_includes_gst": True, "gst_on_sale": "150.00",
            "sale_price_ex_gst": "1500.00", "gain_or_loss": "500.00", "outcome": "gain"})
    )
    async with _client() as c:
        r = await c.get(f"/fixed-assets/{_ASSET_ID}/disposal-preview"
                        "?disposal_date=2026-11-01&proceeds=1650&gst_applies=false")
        q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
        assert q["gst_applies"] == ["false"]
        r = await c.get(f"/fixed-assets/{_ASSET_ID}/disposal-preview"
                        "?disposal_date=2026-11-01&proceeds=1650")
        q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
        assert "gst_applies" not in q
    html = r.text
    assert "Disposing posts these figures" in html
    assert "the GST (on the BAS with the sale price)" in html
    assert "does not post" not in html


# ---------------------------------------------------------------------------
# Settle only after lodged
# ---------------------------------------------------------------------------


def test_settings_form_round_trips_the_lodged_setting() -> None:
    form = _prefill_form({"name": "Co", "version": 1, "bas_settle_requires_lodged": True})
    assert form["bas_settle_requires_lodged"] == "true"
    form = _prefill_form({"name": "Co", "version": 1})
    assert form["bas_settle_requires_lodged"] == ""

    on = _build_company_payload({"name": "Co"}, {"bas_settle_requires_lodged": "true"},
                                mode="full", jurisdiction="AU")
    assert on["bas_settle_requires_lodged"] is True
    off = _build_company_payload({"name": "Co"}, {}, mode="full", jurisdiction="AU")
    assert off["bas_settle_requires_lodged"] is False
    # Not an Australian company: the GST section is not shown, nothing sent.
    ee = _build_company_payload({"name": "Co"}, {}, mode="full", jurisdiction="EE")
    assert "bas_settle_requires_lodged" not in ee


@pytest.mark.anyio
@respx.mock
async def test_settings_page_shows_the_lodged_setting(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/.*$").mock(
        return_value=Response(200, json={"items": [{
            "id": _COMPANY_ID, "name": "Test Co", "version": 2, "tax_registered": True,
            "bas_settle_requires_lodged": True}]})
    )
    async with _client() as c:
        r = await c.get("/settings/company")
    assert r.status_code == 200, r.text
    assert "Only allow a BAS to be settled after it is marked lodged" in r.text
    tag = r.text.split('id="bas_settle_requires_lodged"', 1)[1].split(">", 1)[0]
    assert "checked" in tag


_MESSAGE = ('This BAS period is not marked lodged yet. Mark it lodged first '
            '(Settings: "Only allow a BAS to be settled after it is marked lodged" is on).')


@pytest.mark.anyio
@respx.mock
async def test_settle_screen_explains_and_links_to_mark_lodged(
    respx_mock: respx.MockRouter,
) -> None:
    plan = {
        "from_date": "2037-03-01", "to_date": "2037-03-31",
        "lines": [{"account_id": "a1", "account_code": "2-1310", "account_name": "GST Collected",
                   "description": "Clear GST on sales (1A)", "debit": "60.00", "credit": "0"}],
        "ato_account": {"id": "ato", "code": "2-1340", "name": "ATO"},
        "amount_payable": "60.00", "result": "PAYABLE", "problems": [_MESSAGE],
        "already_settled": [], "can_settle": False, "needs_lodging": True,
    }
    respx_mock.get(url__regex=rf"^{_API}/api/v1/bas/settle/preview.*$").mock(
        return_value=Response(200, json=plan))
    respx_mock.get(url__regex=rf"^{_API}/api/v1/bank_accounts.*$").mock(
        return_value=Response(200, json={"items": []}))
    async with _client() as c:
        r = await c.get("/bas/settle?from_date=2037-03-01&to_date=2037-03-31")
    assert r.status_code == 200
    html = r.text
    assert "not marked lodged yet" in html
    assert 'href="/bas#bas-2037-03-01"' in html
    assert "Mark this BAS lodged" in html
    assert 'action="/bas/settle"' not in html  # no settle button


@pytest.mark.anyio
@respx.mock
async def test_bas_periods_have_an_anchor_per_period(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/bas/periods").mock(return_value=Response(200, json={
        "basis": "accrual", "frequency": "quarterly", "waiting": [],
        "items": [{"period_id": "p1", "from_date": "2037-01-01", "to_date": "2037-03-31",
                   "label": "January to March 2037", "status": "open",
                   "due_date": "2037-04-28", "return": None, "settlements": []}],
    }))
    async with _client() as c:
        r = await c.get("/bas")
    assert r.status_code == 200
    assert 'id="bas-2037-01-01"' in r.text
