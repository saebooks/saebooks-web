"""Batch 10 — foreign currency screens.

* fx_context: company currency from the active company, picker on paid plans.
* /fx/lookup proxies the engine's rate lookup (prefill + source).
* rate screen: list, save, remove, fetch latest Reserve Bank rates.
* month-end revaluation post button.
* invoice / bill / expense / payment / bank account forms send the
  currency, rate, bank amount and fee the engine expects — a blank rate on
  a foreign document is left for the engine to look up, never sent as 1.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode
from types import SimpleNamespace

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.fx_context import company_currency, contact_currencies, fx_context
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_CONTACT = "11111111-1111-1111-1111-111111111111"
_ACCOUNT = "22222222-2222-2222-2222-222222222222"
_BANK = "33333333-3333-3333-3333-333333333333"
_DOC = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode())).decode()


_SESSION = _cookie({"api_token": "test-token", "user_role": "owner"})


def _client(**kw) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION},
        follow_redirects=False,
        **kw,
    )


def _csrf(html: str) -> str:
    import re

    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def _fallback(respx_mock: respx.MockRouter) -> None:
    """Everything not under test answers with an empty list."""
    respx_mock.route(host="localhost").mock(
        return_value=Response(200, json={"items": [], "total": 0, "limit": 500, "offset": 0})
    )


@pytest.fixture(autouse=True)
def _enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "enterprise")


# ---------------------------------------------------------------------------
# fx_context
# ---------------------------------------------------------------------------


def _req(base: str | None = "EUR", jp_default: str = "AUD") -> SimpleNamespace:
    state = SimpleNamespace(
        active_company_id="c1",
        companies=[{"id": "c1", "base_currency": base}] if base else [],
        jp=SimpleNamespace(default_currency=jp_default),
    )
    return SimpleNamespace(state=state, scope={}, session={})


def test_fx_context_uses_the_active_company_currency() -> None:
    ctx = fx_context(_req("EUR"))
    assert ctx["base"] == "EUR"
    assert ctx["currencies"][0]["code"] == "EUR"
    assert ctx["enabled"] is True
    assert company_currency(_req(None, "NZD")) == "NZD"
    assert company_currency(_req("XXX", "AUD")) == "AUD"


def test_fx_context_hides_picker_on_free_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "community")
    assert fx_context(_req("AUD"))["enabled"] is False


def test_contact_currencies_map() -> None:
    assert contact_currencies(
        [{"id": "a", "currency_code": "usd"}, {"id": "b", "currency_code": None}]
    ) == {"a": "USD"}


# ---------------------------------------------------------------------------
# Rate lookup + rate screen
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_lookup_returns_rate_and_source(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API}/api/v1/fx/rate").mock(
        return_value=Response(
            200,
            json={
                "currency": "EUR",
                "base_currency": "AUD",
                "rate": "1.60000000",
                "inverse": "0.625",
                "source": "rba",
                "source_label": "Reserve Bank of Australia, 3 Mar 2031",
                "rate_date": "2031-03-03",
                "requested_date": "2031-03-05",
            },
        )
    )
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.get("/fx/lookup", params={"currency": "eur", "on": "2031-03-05"})
    assert resp.status_code == 200
    assert resp.json()["rate"] == "1.60000000"
    assert "Reserve Bank" in resp.json()["source_label"]
    assert route.calls.last.request.url.params["currency"] == "EUR"
    assert route.calls.last.request.url.params["date"] == "2031-03-05"


@pytest.mark.anyio
@respx.mock
async def test_lookup_passes_the_engine_message_when_no_rate(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/fx/rate").mock(
        return_value=Response(404, json={"detail": "There is no exchange rate for EUR. Enter the rate."})
    )
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.get("/fx/lookup", params={"currency": "EUR"})
    assert resp.status_code == 404
    assert "Enter the rate" in resp.json()["error"]


@pytest.mark.anyio
@respx.mock
async def test_rate_screen_lists_and_saves(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/fx/rates").mock(
        return_value=Response(
            200,
            json={
                "base_currency": "AUD",
                "items": [
                    {"id": _DOC, "currency": "EUR", "rate_date": "2031-03-03",
                     "rate": "1.62000000", "note": "bank quote", "created_by": "x"}
                ],
            },
        )
    )
    saved = respx_mock.post(f"{_API}/api/v1/fx/rates").mock(
        return_value=Response(201, json={"id": _DOC})
    )
    refresh = respx_mock.post(f"{_API}/api/v1/fx/rates/refresh").mock(
        return_value=Response(200, json={"loaded": 40, "latest_date": "2031-03-04"})
    )
    _fallback(respx_mock)
    async with _client() as c:
        page = await c.get("/settings/exchange-rates")
        assert page.status_code == 200
        assert "Exchange rates" in page.text
        assert "1 EUR = 1.62000000 AUD" in page.text
        assert "bank quote" in page.text
        assert "dark:bg-gray-800" in page.text  # dark theme classes present
        csrf = _csrf(page.text)
        resp = await c.post(
            "/settings/exchange-rates",
            data={"currency": "eur", "rate_date": "2031-03-05", "rate": "1.63",
                  "note": "", "csrf_token": csrf},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 303
        resp = await c.post(
            "/settings/exchange-rates/refresh",
            data={"csrf_token": csrf},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 303
    body = _json.loads(saved.calls.last.request.content)
    assert body == {"currency": "EUR", "rate_date": "2031-03-05", "rate": "1.63", "note": None}
    assert refresh.called


@pytest.mark.anyio
@respx.mock
async def test_revaluation_post_button(respx_mock: respx.MockRouter) -> None:
    posted = respx_mock.post(f"{_API}/api/v1/fx/revaluation").mock(
        return_value=Response(
            200,
            json={"through_date": "2031-03-31", "posted": [{"adjustment_entry_id": "a", "reversal_entry_id": "b"}],
                  "skipped_currencies": [], "zero_currencies": [], "repaired_currencies": []},
        )
    )
    _fallback(respx_mock)
    async with _client() as c:
        page = await c.get("/settings/exchange-rates")
        csrf = _csrf(page.text)
        resp = await c.post(
            "/reports/fx-revaluation/post",
            data={"through_date": "2031-03-31", "csrf_token": csrf},
            headers={"X-CSRF-Token": csrf},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/fx-revaluation?as_of_date=2031-03-31"
    assert _json.loads(posted.calls.last.request.content) == {"through_date": "2031-03-31"}


@pytest.mark.anyio
@respx.mock
async def test_revaluation_page_shows_currency_preview(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/reports/fx_revaluation").mock(
        return_value=Response(200, json={"as_of_date": "2031-03-31", "base_currency": "AUD",
                                         "items": [], "total_items": 0, "note": "",
                                         "total_booked_base": 0, "total_revalued_base": 0})
    )
    respx_mock.get(f"{_API}/api/v1/fx/revaluation").mock(
        return_value=Response(200, json={
            "through_date": "2031-03-31", "base_currency": "AUD", "can_post": True,
            "total_gain_or_loss": "100.00",
            "currencies": [{
                "currency": "EUR", "rate": "1.7", "rate_source": "Your rate for 31 Mar 2031",
                "rate_date": "2031-03-31", "outstanding_foreign_receivable": "1000.00",
                "booked_receivable": "1600.00", "revalued_receivable": "1700.00",
                "receivable_change": "100.00", "outstanding_foreign_payable": "0",
                "booked_payable": "0", "revalued_payable": "0", "payable_change": "0",
                "gain_or_loss": "100.00", "already_posted": False,
                "adjustment_entry_id": None, "problem": None}],
        })
    )
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.get("/reports/fx-revaluation", params={"as_of_date": "2031-03-31"})
    assert resp.status_code == 200
    assert "Post revaluation" in resp.text
    assert "1 EUR = 1.7 AUD" in resp.text
    assert "Your rate for 31 Mar 2031" in resp.text


# ---------------------------------------------------------------------------
# Document forms send what the engine expects
# ---------------------------------------------------------------------------


def _capture(respx_mock: respx.MockRouter, path: str, body: dict) -> list[dict]:
    seen: list[dict] = []

    def _side(request):
        seen.append(_json.loads(request.content))
        return Response(201, json=body)

    respx_mock.post(f"{_API}{path}").mock(side_effect=_side)
    return seen


_LINE = {
    "lines[0][account_id]": _ACCOUNT,
    "lines[0][description]": "Parts",
    "lines[0][quantity]": "1",
    "lines[0][unit_price]": "1000",
}


@pytest.mark.anyio
@respx.mock
async def test_foreign_bill_without_rate_lets_the_engine_look_it_up(respx_mock: respx.MockRouter) -> None:
    seen = _capture(respx_mock, "/api/v1/bills", {"id": _DOC, "version": 1})
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.post("/bills/new", data={
            "contact_id": _CONTACT, "issue_date": "2031-03-05", "due_date": "2031-04-05",
            "currency": "EUR", "fx_rate": "", "idempotency_key": "k1", **_LINE,
        })
    assert resp.status_code == 303, resp.text
    assert seen[0]["currency"] == "EUR"
    assert "fx_rate" not in seen[0], "a blank rate must not be sent (no silent 1.00)"


@pytest.mark.anyio
@respx.mock
async def test_invoice_sends_currency_and_rate(respx_mock: respx.MockRouter) -> None:
    seen = _capture(respx_mock, "/api/v1/invoices", {"id": _DOC, "version": 1})
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.post("/invoices/new", data={
            "contact_id": _CONTACT, "issue_date": "2031-03-05", "due_date": "2031-04-05",
            "currency": "usd", "fx_rate": "1.55", "idempotency_key": "k2", **_LINE,
        })
    assert resp.status_code == 303, resp.text
    assert seen[0]["currency"] == "USD"
    assert seen[0]["fx_rate"] == "1.55"


@pytest.mark.anyio
@respx.mock
async def test_aud_invoice_sends_no_rate(respx_mock: respx.MockRouter) -> None:
    seen = _capture(respx_mock, "/api/v1/invoices", {"id": _DOC, "version": 1})
    _fallback(respx_mock)
    async with _client() as c:
        await c.post("/invoices/new", data={
            "contact_id": _CONTACT, "issue_date": "2031-03-05", "due_date": "2031-04-05",
            "currency": "AUD", "fx_rate": "1.55", "idempotency_key": "k3", **_LINE,
        })
    assert seen[0]["currency"] == "AUD"
    assert "fx_rate" not in seen[0]


@pytest.mark.anyio
@respx.mock
async def test_foreign_payment_sends_the_bank_amount(respx_mock: respx.MockRouter) -> None:
    seen = _capture(respx_mock, "/api/v1/payments", {"id": _DOC, "version": 1})
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.post("/payments/new", data={
            "contact_id": _CONTACT, "payment_date": "2031-03-15", "amount": "1000.00",
            "direction": "OUTGOING", "method": "eft", "bank_account_id": _BANK,
            "currency": "EUR", "base_amount": "1650.00", "fx_rate": "1.65",
            "idempotency_key": "k4",
            "allocations[0][target_type]": "BILL", "allocations[0][target_id]": _DOC,
            "allocations[0][amount]": "1000.00",
        })
    assert resp.status_code == 303, resp.text
    assert seen[0]["currency"] == "EUR"
    assert seen[0]["base_amount"] == "1650.00"
    assert "fx_rate" not in seen[0], "the bank amount sets the rate"


@pytest.mark.anyio
@respx.mock
async def test_expense_sends_conversion_fee(respx_mock: respx.MockRouter) -> None:
    seen = _capture(respx_mock, "/api/v1/expenses", {"id": _DOC, "version": 1})
    _fallback(respx_mock)
    async with _client() as c:
        await c.post("/expenses/new", data={
            "payment_account_id": _BANK, "expense_date": "2031-03-05",
            "currency": "USD", "fx_rate": "1.5",
            "fx_fee_amount": "4.20", "fx_fee_account_id": _ACCOUNT,
            "idempotency_key": "k5", **_LINE,
        })
    assert seen[0]["currency"] == "USD"
    assert seen[0]["fx_fee_amount"] == "4.20"
    assert seen[0]["fx_fee_account_id"] == _ACCOUNT


@pytest.mark.anyio
@respx.mock
async def test_bank_account_currency_is_sent(respx_mock: respx.MockRouter) -> None:
    seen = _capture(respx_mock, "/api/v1/bank_accounts", {"id": _DOC, "version": 1})
    _fallback(respx_mock)
    async with _client() as c:
        await c.post("/bank-accounts/new", data={
            "code": "1-1190", "name": "USD account", "account_kind": "BANK_CHECKING",
            "currency": "USD", "idempotency_key": "k6",
        })
    assert seen and seen[0]["currency"] == "USD"


@pytest.mark.anyio
@respx.mock
async def test_invoice_form_has_picker_and_contact_currencies(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [
            {"id": _CONTACT, "name": "Berlin GmbH", "contact_type": "CUSTOMER", "currency_code": "EUR"}
        ], "total": 1})
    )
    _fallback(respx_mock)
    async with _client() as c:
        resp = await c.get("/invoices/new")
    assert resp.status_code == 200
    assert "data-fx-root" in resp.text
    assert 'name="fx_rate"' in resp.text
    assert "EUR — Euro" in resp.text
    assert f'"{_CONTACT}": "EUR"' in resp.text
    assert "/static/js/fx_fields.js" in resp.text
