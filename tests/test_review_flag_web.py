"""Tests for the "flag for review" UI (Gap 3, 0157) in saebooks-web.

Covers the three entities that ship the review-flag endpoint
(invoices / expenses / journal entries):

  * list page renders the flag control / column header
  * the "Flagged only" filter forwards ?flagged=true to the API
  * detail page renders the labelled flag control
  * POST /{base}/{id}/review-flag?flagged=true proxies to the API and
    returns the swapped partial (set + clear)
  * compact vs labelled variant is honoured via ?compact=

The web app talks to the API over httpx; respx mocks those calls.
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

_INVOICE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_EXPENSE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ENTRY_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_CONTACT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

_API_BASE = settings.api_url.rstrip("/")


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-flag"})


_MOCK_INVOICE = {
    "id": _INVOICE_ID,
    "company_id": "11111111-1111-1111-1111-111111111111",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "contact_id": _CONTACT_ID,
    "number": "INV-0042",
    "issue_date": "2026-04-01",
    "due_date": "2026-04-30",
    "status": "POSTED",
    "subtotal": "1000.00",
    "tax_total": "100.00",
    "total": "1100.00",
    "amount_paid": "0.00",
    "currency": "AUD",
    "fx_rate": "1.0",
    "notes": None,
    "payment_terms": "Net 30",
    "posted_at": "2026-04-01T10:00:00Z",
    "posted_by": "api:testuser",
    "version": 1,
    "created_at": "2026-04-01T09:00:00Z",
    "updated_at": "2026-04-01T10:00:00Z",
    "archived_at": None,
    "flagged_for_review": False,
    "review_note": None,
    "lines": [],
}

_MOCK_INVOICES_RESPONSE = {"items": [_MOCK_INVOICE], "total": 1, "limit": 50, "offset": 0}

_MOCK_EXPENSE = {
    "id": _EXPENSE_ID,
    "number": "EXP-0007",
    "expense_date": "2026-04-02",
    "reference": "REF-1",
    "status": "POSTED",
    "subtotal": "50.00",
    "tax_total": "5.00",
    "total": "55.00",
    "currency": "AUD",
    "contact_id": None,
    "payment_account_id": None,
    "notes": None,
    "archived_at": None,
    "journal_entry_id": None,
    "void_journal_entry_id": None,
    "flagged_for_review": False,
    "review_note": None,
    "lines": [],
}

_MOCK_EXPENSES_RESPONSE = {"items": [_MOCK_EXPENSE], "total": 1, "limit": 50, "offset": 0}

_MOCK_ENTRY = {
    "id": _ENTRY_ID,
    "ref": "JE-0003",
    "entry_date": "2026-04-03",
    "description": "Accrual",
    "status": "POSTED",
    "version": 2,
    "posted_at": "2026-04-03T10:00:00Z",
    "posted_by": "api:testuser",
    "flagged_for_review": False,
    "review_note": None,
    "lines": [
        {"account_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "debit": "100.00", "credit": "0.00"},
        {"account_id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "debit": "0.00", "credit": "100.00"},
    ],
}

_MOCK_ENTRIES_RESPONSE = {"items": [_MOCK_ENTRY], "total": 1, "limit": 50, "offset": 0}


# ---------------------------------------------------------------------------
# List + detail render the flag control
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoices_list_has_flag_control(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/invoices")
    assert resp.status_code == 200
    # The per-row toggle posts to the review-flag endpoint, and the filter exists.
    assert f"/invoices/{_INVOICE_ID}/review-flag" in resp.text
    assert "Flagged only" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoices_list_flagged_filter_forwards(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/invoices?flagged=true")
    assert resp.status_code == 200
    # The upstream API call must carry flagged=true.
    assert route.called
    assert route.calls.last.request.url.params.get("flagged") == "true"


@pytest.mark.anyio
@respx.mock
async def test_invoices_list_no_flagged_param_when_off(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=_MOCK_INVOICES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        await client.get("/invoices")
    assert route.called
    assert "flagged" not in route.calls.last.request.url.params


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_has_labelled_flag(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=_MOCK_INVOICE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")
    assert resp.status_code == 200
    assert "Flag for review" in resp.text  # labelled (not flagged yet) button


@pytest.mark.anyio
@respx.mock
async def test_invoice_detail_shows_flagged_badge(respx_mock: respx.MockRouter) -> None:
    flagged = {**_MOCK_INVOICE, "flagged_for_review": True, "review_note": "check GST"}
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}").mock(
        return_value=Response(200, json=flagged)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(
        return_value=Response(200, json=[])
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/invoices/{_INVOICE_ID}")
    assert resp.status_code == 200
    assert "Flagged for review" in resp.text
    assert "check GST" in resp.text
    assert "Clear flag" in resp.text


# ---------------------------------------------------------------------------
# POST toggle proxies to the API and returns the swapped partial
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_set_flag_returns_partial(respx_mock: respx.MockRouter) -> None:
    flagged = {**_MOCK_INVOICE, "flagged_for_review": True, "review_note": None}
    route = respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/review-flag").mock(
        return_value=Response(200, json=flagged)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/review-flag?flagged=true&compact=false",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    # Upstream got the JSON body with flagged=true.
    assert route.called
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"flagged": True}
    # Returned fragment is the labelled "flagged" state (clear button present),
    # not a full page.
    assert "<html" not in resp.text
    assert "Clear flag" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_invoice_clear_flag_compact_returns_icon(respx_mock: respx.MockRouter) -> None:
    cleared = {**_MOCK_INVOICE, "flagged_for_review": False, "review_note": None}
    route = respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_INVOICE_ID}/review-flag").mock(
        return_value=Response(200, json=cleared)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/invoices/{_INVOICE_ID}/review-flag?flagged=false&compact=true",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert route.called
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"flagged": False}
    # Compact (icon) variant — the "set" button posts flagged=true.
    assert "review-flag?flagged=true&amp;compact=true" in resp.text or \
           "review-flag?flagged=true&compact=true" in resp.text
    assert "<html" not in resp.text


@pytest.mark.anyio
async def test_invoice_review_flag_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post(f"/invoices/{_INVOICE_ID}/review-flag?flagged=true")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_expenses_list_flagged_filter_forwards(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{_API_BASE}/api/v1/expenses").mock(
        return_value=Response(200, json=_MOCK_EXPENSES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": []})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/expenses?flagged=true")
    assert resp.status_code == 200
    assert route.called
    assert route.calls.last.request.url.params.get("flagged") == "true"
    assert "Flagged only" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_expense_set_flag(respx_mock: respx.MockRouter) -> None:
    flagged = {**_MOCK_EXPENSE, "flagged_for_review": True}
    route = respx_mock.post(f"{_API_BASE}/api/v1/expenses/{_EXPENSE_ID}/review-flag").mock(
        return_value=Response(200, json=flagged)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/expenses/{_EXPENSE_ID}/review-flag?flagged=true&compact=true",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert route.called
    assert _json.loads(route.calls.last.request.content) == {"flagged": True}


@pytest.mark.anyio
@respx.mock
async def test_expense_detail_has_flag(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/expenses/{_EXPENSE_ID}").mock(
        return_value=Response(200, json=_MOCK_EXPENSE)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/expenses/{_EXPENSE_ID}")
    assert resp.status_code == 200
    assert "Flag for review" in resp.text


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_journal_entries_list_flagged_filter_forwards(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(f"{_API_BASE}/api/v1/journal_entries").mock(
        return_value=Response(200, json=_MOCK_ENTRIES_RESPONSE)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": []})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/journal_entries/_filter_options").mock(
        return_value=Response(200, json={"posted_by": [], "ref_prefixes": []})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/journal-entries?flagged=true")
    assert resp.status_code == 200
    assert route.called
    assert route.calls.last.request.url.params.get("flagged") == "true"
    assert "Flagged only" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_journal_entry_set_flag(respx_mock: respx.MockRouter) -> None:
    flagged = {**_MOCK_ENTRY, "flagged_for_review": True}
    route = respx_mock.post(
        f"{_API_BASE}/api/v1/journal_entries/{_ENTRY_ID}/review-flag"
    ).mock(return_value=Response(200, json=flagged))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            f"/journal-entries/{_ENTRY_ID}/review-flag?flagged=true&compact=false",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert route.called
    assert _json.loads(route.calls.last.request.content) == {"flagged": True}
    assert "Clear flag" in resp.text
