"""Batch 15 of the 2026-11-13 capability sweep: phone and polish (web side).

Covers the needs-attention card (P1-32), tap to call and the contact action
row (P1-75, P1-76), list tables as cards on a phone (P1-77) and the P2 rows
left to this batch: purchase tax codes on expenses (P2-2), the year-end
checklist and lock reason (P2-3), the asset disposal preview (P2-4), the
cash forecast (P2-5), aged by invoice (P2-6), bad date links and "today"
(P2-7), saved display preferences (P2-9), the written-off filter (P2-12),
the duplicate bill warning (P2-13), casual loading (P2-19), the ready to pay
checklist (P2-20), payroll accounts in one step (P2-21), save and post from
the inbox (P2-23), the email-in address (P2-24), the phone app shell
(P2-25) and contact people, notes and emails (P2-26).

The engine is mocked with respx; nothing here talks to a real engine.
"""
from __future__ import annotations

import json as _json
import re
from base64 import b64encode as _b64encode
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")

_CONTACT_ID = "c0c0c0c0-0000-4000-8000-000000000001"
_PERSON_ID = "p0p0p0p0-0000-4000-8000-000000000002".replace("p", "a")
_NOTE_ID = "b0b0b0b0-0000-4000-8000-000000000003"
_BILL_ID = "d0d0d0d0-0000-4000-8000-000000000004"
_ASSET_ID = "e0e0e0e0-0000-4000-8000-000000000005"
_EMP_ID = "f0f0f0f0-0000-4000-8000-000000000006"
_DOC_ID = "a1a1a1a1-0000-4000-8000-000000000007"
_EXP_ID = "a2a2a2a2-0000-4000-8000-000000000008"
_COMPANY_ID = "a3a3a3a3-0000-4000-8000-000000000009"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode("utf-8"))).decode("utf-8")


_ADMIN = _cookie({"api_token": "test-token-b15", "user_role": "admin", "is_sae_staff": False})
_BOOKKEEPER = _cookie({"api_token": "test-token-b15", "user_role": "bookkeeper", "is_sae_staff": False})


def _client(cookie: str = _ADMIN, follow: bool = False, extra_cookies: dict | None = None) -> AsyncClient:
    cookies = {settings.session_cookie_name: cookie}
    cookies.update(extra_cookies or {})
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
        follow_redirects=follow,
    )


# ---------------------------------------------------------------------------
# P1-32 needs-attention card
# ---------------------------------------------------------------------------

_ATTENTION = {
    "as_at": "2026-11-13",
    "total_count": 9,
    "items": [
        {"key": "draft_bills", "count": 0, "amount": None, "oldest": None},
        {"key": "bank_lines_to_reconcile", "count": 4, "amount": None, "oldest": "2026-10-02"},
        {"key": "overdue_invoices", "count": 3, "amount": "1513.59", "oldest": "2026-09-30"},
        {"key": "bills_due_this_week", "count": 2, "amount": "880.00", "oldest": "2026-11-14"},
        {"key": "inbox_waiting", "count": 0, "amount": None, "oldest": None},
    ],
}


@pytest.mark.anyio
@respx.mock
async def test_attention_card_lists_waiting_work_in_order() -> None:
    respx.get(f"{_API}/api/v1/attention").mock(return_value=Response(200, json=_ATTENTION))
    async with _client() as c:
        resp = await c.get("/dashboard/attention")
    assert resp.status_code == 200
    html = resp.text
    assert "Needs attention" in html
    # Zero-count items are left out.
    assert "Draft bills not entered" not in html
    assert "Receipts and bills in the inbox" not in html
    # Money owed to you first, then bills, then the bank.
    keys = re.findall(r'data-attention-key="([a-z_]+)"', html)
    assert keys == ["overdue_invoices", "bills_due_this_week", "bank_lines_to_reconcile"]
    assert 'href="/invoices?status=overdue"' in html
    assert 'href="/bills?status=due-soon"' in html
    assert "1,513.59" in html
    # Count-only snapshot for the offline page, no amounts.
    assert "sb_attention_snapshot" in html


@pytest.mark.anyio
@respx.mock
async def test_attention_card_nothing_waiting() -> None:
    respx.get(f"{_API}/api/v1/attention").mock(
        return_value=Response(200, json={"as_at": "2026-11-13", "total_count": 0, "items": []})
    )
    async with _client() as c:
        resp = await c.get("/dashboard/attention")
    assert "Nothing waiting" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_attention_card_engine_failure_never_breaks_dashboard() -> None:
    respx.get(f"{_API}/api/v1/attention").mock(return_value=Response(500, json={}))
    async with _client() as c:
        resp = await c.get("/dashboard/attention")
    assert resp.status_code == 200
    assert "Could not check what needs attention" in resp.text


@pytest.mark.anyio
async def test_attention_card_requires_login() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/dashboard/attention")
    assert resp.status_code in (303, 401)


def test_dashboard_template_loads_attention_card_first() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()
    slot = src.index('hx-get="/dashboard/attention"')
    assert slot < src.index('data-widget-zone="kpi"')


# ---------------------------------------------------------------------------
# P1-75 / P1-76 / P2-26 contact page
# ---------------------------------------------------------------------------


def _render_contact_actions(contact: dict) -> str:
    from pathlib import Path

    from fastapi.templating import Jinja2Templates

    from saebooks_web.i18n import gettext

    env = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates")).env
    env.globals.setdefault("_", gettext)
    return env.get_template("b15/contact_actions.html").render(contact=contact)


def test_contact_actions_customer_tap_to_call_and_prefilled_links() -> None:
    html = _render_contact_actions(
        {"id": _CONTACT_ID, "contact_type": "CUSTOMER", "phone": "07 4243 0000",
         "email": "accounts@example.com", "archived_at": None}
    )
    assert 'href="tel:0742430000"' in html
    assert 'href="mailto:accounts@example.com"' in html
    assert f'/invoices/new?contact_id={_CONTACT_ID}' in html
    assert f'/quotes/new?contact_id={_CONTACT_ID}' in html
    assert f'/payments/new?direction=INCOMING&amp;contact_id={_CONTACT_ID}' in html
    assert f'/contacts/{_CONTACT_ID}/statement/pdf' in html
    # Supplier-only actions are not offered for a customer.
    assert "/bills/new" not in html


def test_contact_actions_supplier_and_archived() -> None:
    html = _render_contact_actions(
        {"id": _CONTACT_ID, "contact_type": "SUPPLIER", "phone": None, "email": None, "archived_at": None}
    )
    assert f'/bills/new?contact_id={_CONTACT_ID}' in html
    assert f'/payments/new?direction=OUTGOING&amp;contact_id={_CONTACT_ID}' in html
    assert "/invoices/new" not in html
    assert "tel:" not in html
    archived = _render_contact_actions(
        {"id": _CONTACT_ID, "contact_type": "BOTH", "phone": "1", "email": None, "archived_at": "2026-01-01"}
    )
    assert "data-contact-actions" not in archived


def test_contact_detail_template_wires_actions_hub_and_tel() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "contacts" / "detail.html").read_text()
    assert '{% include "b15/contact_actions.html" %}' in src
    assert 'hx-get="/contacts/{{ contact.id }}/hub"' in src
    assert "tel:{{ contact.phone" in src


_HUB_PEOPLE = {"items": [{"id": _PERSON_ID, "contact_id": _CONTACT_ID, "name": "Jo Accounts",
                          "role": "Accounts", "email": "jo@example.com", "phone": "0400 000 000",
                          "receives_documents": True, "notes": None, "active": True}]}
_HUB_NOTES = {"items": [{"id": _NOTE_ID, "contact_id": _CONTACT_ID, "note_date": "2026-11-12",
                         "body": "Rang about 1042, paying Friday", "created_by": "richard"}]}
_HUB_EMAILS = {"items": [{"doc_type": "invoice", "doc_id": _BILL_ID, "doc_number": "INV-1042",
                          "subject": "Invoice 1042", "to": ["jo@example.com"],
                          "sent_at": "2026-11-01T01:00:00Z", "state": "bounced",
                          "detail": "Mailbox full"}]}


@pytest.mark.anyio
@respx.mock
async def test_contact_hub_fragment_people_notes_emails() -> None:
    base = f"{_API}/api/v1/contact-records/{_CONTACT_ID}"
    respx.get(f"{base}/people").mock(return_value=Response(200, json=_HUB_PEOPLE))
    respx.get(f"{base}/notes").mock(return_value=Response(200, json=_HUB_NOTES))
    respx.get(f"{base}/communications").mock(return_value=Response(200, json=_HUB_EMAILS))
    async with _client() as c:
        resp = await c.get(f"/contacts/{_CONTACT_ID}/hub")
    html = resp.text
    assert resp.status_code == 200
    assert "Jo Accounts" in html and 'href="tel:0400000000"' in html
    assert "Gets a copy of invoices and statements" in html
    assert "Rang about 1042, paying Friday" in html
    # A bounced email is shown as bounced, not "Sent".
    assert "Bounced" in html
    assert f'href="/invoices/{_BILL_ID}"' in html
    assert "csrf_token" in html


@pytest.mark.anyio
@respx.mock
async def test_contact_hub_add_person_posts_to_engine() -> None:
    route = respx.post(f"{_API}/api/v1/contact-records/{_CONTACT_ID}/people").mock(
        return_value=Response(201, json=_HUB_PEOPLE["items"][0])
    )
    async with _client() as c:
        resp = await c.post(
            f"/contacts/{_CONTACT_ID}/people",
            data={"name": " Jo Accounts ", "email": "jo@example.com", "receives_documents": "1"},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/contacts/{_CONTACT_ID}#contact-hub"
    sent = _json.loads(route.calls.last.request.content)
    assert sent["name"] == "Jo Accounts"
    assert sent["receives_documents"] is True
    assert sent["phone"] is None


@pytest.mark.anyio
@respx.mock
async def test_contact_hub_add_person_without_name_is_refused() -> None:
    route = respx.post(f"{_API}/api/v1/contact-records/{_CONTACT_ID}/people")
    async with _client() as c:
        resp = await c.post(f"/contacts/{_CONTACT_ID}/people", data={"name": "  "})
    assert resp.status_code == 303
    assert not route.called


@pytest.mark.anyio
@respx.mock
async def test_contact_hub_add_and_delete_note() -> None:
    add = respx.post(f"{_API}/api/v1/contact-records/{_CONTACT_ID}/notes").mock(
        return_value=Response(201, json=_HUB_NOTES["items"][0])
    )
    delete = respx.delete(f"{_API}/api/v1/contact-records/{_CONTACT_ID}/notes/{_NOTE_ID}").mock(
        return_value=Response(204)
    )
    async with _client() as c:
        r1 = await c.post(f"/contacts/{_CONTACT_ID}/notes", data={"body": "Called", "note_date": "2026-11-12"})
        r2 = await c.post(f"/contacts/{_CONTACT_ID}/notes/{_NOTE_ID}/delete", data={})
    assert r1.status_code == 303 and r2.status_code == 303
    assert _json.loads(add.calls.last.request.content) == {"body": "Called", "note_date": "2026-11-12"}
    assert delete.called


@pytest.mark.anyio
@respx.mock
async def test_new_invoice_and_quote_prefill_contact() -> None:
    contact = {"id": _CONTACT_ID, "name": "Prefill Pty Ltd", "contact_type": "CUSTOMER"}
    respx.get(f"{_API}/api/v1/contacts").mock(return_value=Response(200, json={"items": [contact], "total": 1}))
    respx.get(f"{_API}/api/v1/accounts").mock(return_value=Response(200, json={"items": [], "total": 0}))
    respx.get(f"{_API}/api/v1/tax_codes").mock(return_value=Response(200, json={"items": [], "total": 0}))
    respx.get(f"{_API}/api/v1/projects").mock(return_value=Response(200, json={"items": [], "total": 0}))
    respx.route(method="GET").mock(return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        inv = await c.get(f"/invoices/new?contact_id={_CONTACT_ID}")
        quo = await c.get(f"/quotes/new?contact_id={_CONTACT_ID}")
    pattern = re.compile(rf'<option value="{_CONTACT_ID}"\s+selected')
    assert pattern.search(inv.text), "invoice customer not prefilled"
    assert pattern.search(quo.text), "quote customer not prefilled"


# ---------------------------------------------------------------------------
# P1-77 phone tables
# ---------------------------------------------------------------------------


def test_base_loads_phone_assets() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text()
    assert '/static/css/phone.css' in src
    assert '/static/js/phone.js' in src


def test_phone_css_stacks_tables_below_640px() -> None:
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "static" / "css" / "phone.css").read_text()
    assert "@media (max-width: 640px)" in css
    assert "table.stack-on-phone thead { display: none; }" in css
    assert "content: attr(data-label)" in css
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "phone.js").read_text()
    assert "stack-on-phone" in js and "htmx:afterSwap" in js


@pytest.mark.anyio
async def test_static_phone_assets_are_served() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        css = await c.get("/static/css/phone.css")
        js = await c.get("/static/js/phone.js")
        up = await c.get("/static/js/inbox_upload.js")
    assert css.status_code == 200 and js.status_code == 200 and up.status_code == 200


# ---------------------------------------------------------------------------
# P2-7 dates
# ---------------------------------------------------------------------------


def test_clean_query_drops_bad_dates_and_converts_australian_ones() -> None:
    from saebooks_web.date_params import clean_query

    q, dropped = clean_query("from_date=2026-13-40&to_date=30%2F06%2F2026&status=draft")
    params = parse_qs(q)
    assert "from_date" not in params
    assert params["to_date"] == ["2026-06-30"]
    assert params["status"] == ["draft"]
    assert dropped == ["from_date"]
    # Good and blank dates are left exactly as they were.
    assert clean_query("from_date=2026-07-01&to_date=") == ("from_date=2026-07-01&to_date=", [])


def test_default_timezone_is_brisbane(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from saebooks_web.date_params import apply_default_timezone

    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.delenv("SAEBOOKS_WEB_TZ", raising=False)
    try:
        assert apply_default_timezone() == "Australia/Brisbane"
        # Brisbane is UTC+10 all year: no daylight saving.
        assert time.timezone == -36000
        monkeypatch.setenv("SAEBOOKS_WEB_TZ", "Europe/Tallinn")
        monkeypatch.delenv("TZ", raising=False)
        assert apply_default_timezone() == "Europe/Tallinn"
    finally:
        monkeypatch.delenv("SAEBOOKS_WEB_TZ", raising=False)
        monkeypatch.delenv("TZ", raising=False)
        apply_default_timezone()


@pytest.mark.anyio
@respx.mock
async def test_bad_date_link_shows_default_period_with_a_notice() -> None:
    respx.get(f"{_API}/api/v1/reports/cashflow_forecast").mock(
        return_value=Response(200, json={"weeks": [], "items": [], "opening_balance": 0,
                                         "total_inflows": 0, "total_outflows": 0,
                                         "projected_closing": 0, "to_date": "2027-02-11"})
    )
    respx.route(method="GET").mock(return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        resp = await c.get("/reports/cash-forecast?from_date=2026-99-99")
    assert resp.status_code == 200
    assert "A date in the link was not a real date" in resp.text


# ---------------------------------------------------------------------------
# P2-12 written-off filter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_invoice_list_written_off_filter_and_badge() -> None:
    inv = {"id": _BILL_ID, "number": "INV-0007", "status": "WRITTEN_OFF", "contact_id": _CONTACT_ID,
           "issue_date": "2026-05-01", "due_date": "2026-05-31", "total": "500.00",
           "amount_paid": "0.00", "currency": "AUD"}
    route = respx.get(f"{_API}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [inv], "total": 1})
    )
    respx.route(method="GET").mock(return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        resp = await c.get("/invoices?status=written-off")
    assert resp.status_code == 200
    assert parse_qs(urlparse(str(route.calls.last.request.url)).query)["status"] == ["WRITTEN_OFF"]
    assert "Written off" in resp.text
    assert 'value="written-off" selected' in resp.text


def test_written_off_is_its_own_payment_status() -> None:
    from saebooks_web.routes.invoices import _invoice_payment_status

    inv = {"status": "WRITTEN_OFF", "total": "100", "amount_paid": "0", "due_date": "2020-01-01"}
    assert _invoice_payment_status(inv, "2026-11-13", "2026-11-20") == "written-off"


# ---------------------------------------------------------------------------
# P2-13 duplicate bill warning
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_duplicate_bill_warning() -> None:
    route = respx.get(f"{_API}/api/v1/bill-checks/duplicates").mock(
        return_value=Response(200, json={"duplicates": [
            {"id": _BILL_ID, "number": "BILL-0042", "issue_date": "2026-10-01",
             "total": "220.00", "status": "POSTED"}]})
    )
    async with _client() as c:
        resp = await c.get(
            f"/bills/duplicate-check?contact_id={_CONTACT_ID}&supplier_reference=INV-77"
            f"&exclude_bill_id={_ASSET_ID}"
        )
    assert "already has a bill with invoice number INV-77" in resp.text
    assert f'href="/bills/{_BILL_ID}"' in resp.text
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["exclude_bill_id"] == [_ASSET_ID]


@pytest.mark.anyio
@respx.mock
async def test_duplicate_bill_check_quiet_without_supplier_or_match() -> None:
    respx.get(f"{_API}/api/v1/bill-checks/duplicates").mock(
        return_value=Response(200, json={"duplicates": []})
    )
    async with _client() as c:
        none = await c.get(f"/bills/duplicate-check?contact_id={_CONTACT_ID}&supplier_reference=X1")
        no_contact = await c.get("/bills/duplicate-check?contact_id=&supplier_reference=X1")
    assert none.text == "" and no_contact.text == ""


# ---------------------------------------------------------------------------
# P2-4 disposal preview
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_disposal_preview_shows_gst_and_gain() -> None:
    route = respx.get(f"{_API}/api/v1/fixed_assets/{_ASSET_ID}/disposal-preview").mock(
        return_value=Response(200, json={
            "cost": "10000.00", "depreciation_to_catch_up": "250.00",
            "accumulated_depreciation": "6250.00", "book_value": "3750.00",
            "sale_price": "5500.00", "sale_price_includes_gst": True, "gst_on_sale": "500.00",
            "sale_price_ex_gst": "5000.00", "gain_or_loss": "1250.00", "outcome": "gain"})
    )
    async with _client() as c:
        resp = await c.get(
            f"/fixed-assets/{_ASSET_ID}/disposal-preview?disposal_date=2026-11-13&proceeds=$5,500"
        )
    html = resp.text
    assert "Gain on sale" in html and "1,250.00" in html
    assert "GST on the sale" in html
    assert parse_qs(urlparse(str(route.calls.last.request.url)).query)["sale_price"] == ["5500"]


@pytest.mark.anyio
async def test_disposal_preview_bad_price() -> None:
    async with _client() as c:
        resp = await c.get(f"/fixed-assets/{_ASSET_ID}/disposal-preview?disposal_date=2026-11-13&proceeds=abc")
    assert "The price is not a number." in resp.text


# ---------------------------------------------------------------------------
# P2-3 year-end checklist
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_close_checklist_lists_open_work_and_requires_a_tick() -> None:
    route = respx.get(f"{_API}/api/v1/attention/year-end").mock(
        return_value=Response(200, json={
            "from_date": "2025-07-01", "through_date": "2026-06-30", "locked_through": None,
            "already_locked": False, "open_count": 3, "ready": False,
            "items": [{"key": "draft_invoices", "count": 2, "oldest": "2026-03-01"},
                      {"key": "bank_lines_to_reconcile", "count": 1, "oldest": "2026-06-29"},
                      {"key": "draft_pay_runs", "count": 0, "oldest": None}]})
    )
    async with _client() as c:
        resp = await c.get("/reports/close-year/checklist?through=2026-06-30")
    html = resp.text
    assert "Draft invoices dated in the period" in html
    assert "Bank lines not yet reconciled" in html
    assert 'name="checklist_ack"' in html and 'hx-swap-oob="true"' in html
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    # The period defaults to the twelve months ending on the close date.
    assert q["from_date"] == ["2025-07-01"]


@pytest.mark.anyio
@respx.mock
async def test_close_checklist_ready() -> None:
    respx.get(f"{_API}/api/v1/attention/year-end").mock(
        return_value=Response(200, json={"through_date": "2026-06-30", "already_locked": False,
                                         "open_count": 0, "ready": True, "items": []})
    )
    async with _client() as c:
        resp = await c.get("/reports/close-year/checklist?through=2026-06-30")
    assert "Ready to close" in resp.text
    assert "checklist_ack" not in resp.text


def test_close_year_page_loads_checklist() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "reports" / "close_year.html").read_text()
    assert "/reports/close-year/checklist?through={{ through }}" in src
    assert 'id="close-ack-slot"' in src


# ---------------------------------------------------------------------------
# P2-5 / P2-6 report screens
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cash_forecast_page() -> None:
    route = respx.get(f"{_API}/api/v1/reports/cashflow_forecast").mock(
        return_value=Response(200, json={
            "from_date": "2026-11-13", "to_date": "2027-01-12", "opening_balance": 1000.0,
            "total_inflows": 500.0, "total_outflows": 2000.0, "projected_closing": -500.0,
            "items": [{"expected_date": "2026-11-20", "description": "INV-1042", "source": "invoice",
                       "source_id": _BILL_ID, "amount": 500.0}],
            "weeks": [{"start": "2026-11-09", "inflows": 500.0, "outflows": 0.0, "net": 500.0,
                       "running_balance": 1500.0},
                      {"start": "2026-11-16", "inflows": 0.0, "outflows": 2000.0, "net": -2000.0,
                       "running_balance": -500.0}]})
    )
    async with _client() as c:
        resp = await c.get("/reports/cash-forecast?days=60")
    assert resp.status_code == 200
    assert "forecast to go below zero" in resp.text
    assert f'href="/invoices/{_BILL_ID}"' in resp.text
    assert parse_qs(urlparse(str(route.calls.last.request.url)).query)["horizon_days"] == ["60"]


@pytest.mark.anyio
@respx.mock
async def test_aged_detail_page() -> None:
    route = respx.get(f"{_API}/api/v1/reports/aged_detail").mock(
        return_value=Response(200, json={
            "side": "payables", "as_at": "2026-11-13", "age_by": "issue",
            "buckets": [{"key": "current", "label": "0 to 30 days"}],
            "totals": {"current": "220.00"}, "grand_total": "220.00",
            "contacts": [{"contact_id": _CONTACT_ID, "contact_name": "Steel Supplies",
                          "total": "220.00", "buckets": {"current": "220.00"},
                          "documents": [{"id": _BILL_ID, "number": "BILL-9", "issue_date": "2026-11-01",
                                         "due_date": "2026-11-30", "total": "220.00", "paid": "0.00",
                                         "outstanding": "220.00", "age_days": 12, "bucket": "current"}]}]})
    )
    async with _client() as c:
        resp = await c.get("/reports/aged-detail?side=payables&age_by=issue")
    assert "Steel Supplies" in resp.text
    assert f'href="/bills/{_BILL_ID}"' in resp.text
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["side"] == ["payables"] and q["age_by"] == ["issue"]


# ---------------------------------------------------------------------------
# P2-9 preferences
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_display_preferences_saved_as_cookies() -> None:
    async with _client() as c:
        resp = await c.post("/settings/preferences/display", data={"date_format": "dmy", "num_group": "1"})
    assert resp.status_code == 303
    set_cookie = " ".join(resp.headers.get_list("set-cookie"))
    assert "sb_date_format=dmy" in set_cookie
    assert "sb_num_group=1" in set_cookie


@pytest.mark.anyio
async def test_display_preferences_reject_unknown_format() -> None:
    async with _client() as c:
        resp = await c.post("/settings/preferences/display", data={"date_format": "<script>"})
    assert "sb_date_format=iso" in " ".join(resp.headers.get_list("set-cookie"))


def test_force_grouping_preference_groups_numbers() -> None:
    from saebooks_web.i18n.format import force_grouping, money, num

    assert num(12345.5, 2) == "12345.50"
    token = force_grouping.set(True)
    try:
        assert num(12345.5, 2) == "12,345.50"
        assert money(12345.5, grouping=False) == "$12,345.50"
    finally:
        force_grouping.reset(token)


@pytest.mark.anyio
@respx.mock
async def test_timezone_saved_on_company_with_version() -> None:
    respx.get(f"{_API}/api/v1/companies").mock(
        return_value=Response(200, json={"items": [{"id": _COMPANY_ID, "version": 7, "timezone": None}],
                                         "total": 1})
    )
    respx.get(f"{_API}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json={"id": _COMPANY_ID, "version": 7, "timezone": None})
    )
    patch = respx.patch(f"{_API}/api/v1/companies/{_COMPANY_ID}").mock(
        return_value=Response(200, json={"id": _COMPANY_ID, "version": 8, "timezone": "Australia/Perth"})
    )
    async with _client() as c:
        resp = await c.post("/settings/preferences/timezone", data={"timezone": "Australia/Perth"})
    assert resp.status_code == 303
    assert patch.called
    assert _json.loads(patch.calls.last.request.content) == {"timezone": "Australia/Perth"}
    assert patch.calls.last.request.headers["If-Match"] == "7"


@pytest.mark.anyio
@respx.mock
async def test_timezone_change_is_admin_only() -> None:
    patch = respx.patch(url__regex=rf"{re.escape(_API)}/api/v1/companies/.*")
    async with _client(_BOOKKEEPER) as c:
        resp = await c.post("/settings/preferences/timezone", data={"timezone": "Australia/Perth"})
    assert resp.status_code == 303
    assert not patch.called


def test_date_format_attribute_on_html() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text()
    assert "data-date-format" in src and "sb_date_format" in src


# ---------------------------------------------------------------------------
# P2-19 / P2-20 / P2-21 payroll
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_payroll_accounts_page_and_create() -> None:
    respx.get(f"{_API}/api/v1/payroll-setup/accounts").mock(
        return_value=Response(200, json={
            "ready": False, "missing_count": 1,
            "accounts": [
                {"purpose": "Wages expense", "code": "6-1100", "status": "OK", "account_name": "Wages"},
                {"purpose": "Tax withheld owing", "code": "2-1400", "status": "MISSING",
                 "account_name": None, "problem": "Missing"}],
            "owing": [{"code": "2-1410", "name": "Super payable", "purpose": "Super owing", "balance": "812.00"}]})
    )
    create = respx.post(f"{_API}/api/v1/payroll-setup/accounts").mock(
        return_value=Response(200, json={"created": [{"code": "2-1400"}], "still_to_fix": []})
    )
    async with _client() as c:
        page = await c.get("/payroll/accounts")
        made = await c.post("/payroll/accounts", data={})
    assert "Create the 1 missing accounts" in page.text
    assert "812.00" in page.text
    assert made.status_code == 303 and create.called


@pytest.mark.anyio
@respx.mock
async def test_payroll_accounts_create_is_admin_only() -> None:
    create = respx.post(f"{_API}/api/v1/payroll-setup/accounts")
    async with _client(_BOOKKEEPER) as c:
        resp = await c.post("/payroll/accounts", data={})
    assert resp.status_code == 303
    assert not create.called


@pytest.mark.anyio
@respx.mock
async def test_employee_readiness_card() -> None:
    respx.get(f"{_API}/api/v1/payroll-setup/employees/{_EMP_ID}/readiness").mock(
        return_value=Response(200, json={"employee_id": _EMP_ID, "ready": False, "items": [
            {"key": "tfn", "ok": False, "message": "The tax file number fails the ATO check digit test.",
             "level": "blocker"},
            {"key": "bank", "ok": True, "message": "Bank details recorded.", "level": "info"}]})
    )
    async with _client() as c:
        resp = await c.get(f"/employees/{_EMP_ID}/readiness")
    assert "Ready to pay" in resp.text
    assert "fails the ATO check digit test" in resp.text
    assert "Fix the items marked below" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_employee_edit_sends_casual_loading() -> None:
    emp = {"id": _EMP_ID, "version": 3, "contact_id": _CONTACT_ID}
    respx.get(f"{_API}/api/v1/employees/{_EMP_ID}").mock(return_value=Response(200, json=emp))
    patch = respx.patch(f"{_API}/api/v1/employees/{_EMP_ID}").mock(
        return_value=Response(200, json={**emp, "version": 4})
    )
    respx.route(method="GET").mock(return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        await c.post(f"/employees/{_EMP_ID}/edit", data={"version": "3", "casual_loading_pct": "25"})
        await c.post(f"/employees/{_EMP_ID}/edit", data={"version": "4", "casual_loading_pct": ""})
    first = _json.loads(patch.calls[0].request.content)
    second = _json.loads(patch.calls[1].request.content)
    assert first["casual_loading_pct"] == "25"
    assert second["casual_loading_pct"] is None


# ---------------------------------------------------------------------------
# P2-2 expense tax codes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_expense_form_only_offers_purchase_tax_codes() -> None:
    tax = respx.get(f"{_API}/api/v1/tax_codes").mock(
        return_value=Response(200, json={"items": [
            {"id": "t1", "code": "GST", "rate": "10", "usage": "both"}], "total": 1})
    )
    respx.get(f"{_API}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [
            {"id": "a1", "code": "6-2000", "name": "Fuel", "default_tax_code": "GST",
             "account_type": "EXPENSE"}], "total": 1})
    )
    respx.route(method="GET").mock(return_value=Response(200, json={"items": [], "total": 0}))
    async with _client() as c:
        resp = await c.get("/expenses/new")
    assert resp.status_code == 200
    assert parse_qs(urlparse(str(tax.calls.last.request.url)).query)["usage"] == ["purchases"]
    assert 'data-tax-code="GST"' in resp.text
    assert "data-tax-code-select" in resp.text


# ---------------------------------------------------------------------------
# P2-23 / P2-24 / P2-25 inbox and phone shell
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_save_and_post_publishes_then_posts() -> None:
    from saebooks_web.routes.inbox_post import post_published_record

    respx.get(f"{_API}/api/v1/expenses/{_EXP_ID}").mock(
        return_value=Response(200, json={"id": _EXP_ID, "version": 2, "status": "DRAFT"})
    )
    post = respx.post(f"{_API}/api/v1/expenses/{_EXP_ID}/post").mock(
        return_value=Response(200, json={"id": _EXP_ID, "version": 3, "status": "POSTED"})
    )

    _Req = SimpleNamespace(session={})

    import httpx

    async with httpx.AsyncClient(base_url=_API) as client:
        resp = await post_published_record(_Req, client, {"record": {"kind": "EXPENSE", "id": _EXP_ID}})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/expenses/{_EXP_ID}"
    assert post.calls.last.request.headers["If-Match"] == "2"
    assert "Posted the expense" in _Req.session["flash"]


@pytest.mark.anyio
@respx.mock
async def test_inbox_save_and_post_keeps_draft_when_refused() -> None:
    from saebooks_web.routes.inbox_post import post_published_record

    respx.get(f"{_API}/api/v1/bills/{_BILL_ID}").mock(
        return_value=Response(200, json={"id": _BILL_ID, "version": 1, "status": "DRAFT"})
    )
    respx.post(f"{_API}/api/v1/bills/{_BILL_ID}/post").mock(
        return_value=Response(409, json={"detail": "The period is locked through 2026-06-30."})
    )

    _Req = SimpleNamespace(session={})

    import httpx

    async with httpx.AsyncClient(base_url=_API) as client:
        resp = await post_published_record(_Req, client, {"record": {"kind": "BILL", "id": _BILL_ID}})
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"
    assert "was not posted: The period is locked" in _Req.session["flash"]


@pytest.mark.anyio
async def test_inbox_credit_note_is_never_auto_posted() -> None:
    from saebooks_web.routes.inbox_post import post_published_record

    _Req = SimpleNamespace(session={})

    resp = await post_published_record(_Req, None, {"record": {"kind": "CREDIT_NOTE", "id": _DOC_ID}})
    assert resp.headers["location"] == f"/credit-notes/{_DOC_ID}"
    assert "not posted automatically" in _Req.session["flash"]


def test_inbox_review_offers_save_and_post() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "inbox" / "review.html").read_text()
    assert 'value="publish_post"' in src
    route_src = (Path(__file__).resolve().parent.parent / "saebooks_web" / "routes" / "inbox.py").read_text()
    assert "post_after_publish" in route_src


@pytest.mark.anyio
@respx.mock
async def test_inbox_email_in_card_states() -> None:
    route = respx.post(f"{_API}/api/v1/inbox/email-addresses/ensure")
    route.side_effect = [
        Response(201, json={"address": "in-abc123@in.example.com"}),
        Response(200, json={"address": None}),
        Response(402, json={"detail": "plan"}),
    ]
    async with _client() as c:
        ready = await c.get("/inbox/_email-in")
        no_domain = await c.get("/inbox/_email-in")
        plan = await c.get("/inbox/_email-in")
    assert "in-abc123@in.example.com" in ready.text
    assert "no mail address set up yet" in no_domain.text
    assert "part of the Business plan" in plan.text


def test_inbox_list_uses_upload_helper_and_email_in() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "templates" / "inbox" / "list.html").read_text()
    assert 'hx-get="/inbox/_email-in"' in src
    assert "/static/js/inbox_upload.js" in src
    assert "window.sbInboxUpload" in src
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "inbox_upload.js").read_text()
    # Resize, retry and keep-for-later are all there.
    assert "MAX_EDGE = 2000" in js
    assert "RETRY_DELAYS" in js
    assert "indexedDB" in js and "'online'" in js


# ---------------------------------------------------------------------------
# Routing: fixed b15 paths are not swallowed by /{id} routes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_b15_fixed_paths_are_not_read_as_an_id() -> None:
    """``/bills/duplicate-check`` and ``/inbox/_email-in`` must reach the b15
    routes, not ``/bills/{bill_id}`` / ``/inbox/{document_id}`` (which would
    422 on a non-UUID id or call the engine for a document)."""
    respx.post(f"{_API}/api/v1/inbox/email-addresses/ensure").mock(
        return_value=Response(201, json={"address": "in-x@in.example.com"})
    )
    async with _client() as c:
        dup = await c.get("/bills/duplicate-check?contact_id=&supplier_reference=")
        email_in = await c.get("/inbox/_email-in")
    assert dup.status_code == 200 and dup.text == ""
    assert email_in.status_code == 200 and "in-x@in.example.com" in email_in.text
