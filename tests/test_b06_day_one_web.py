"""b06 — day one screens: checklist, opening balances, CSV imports, users and
invitations, documents/email settings, numbering, quote email wording.

The engine is mocked with respx; nothing here talks to a real API or sends
real email.
"""
from __future__ import annotations

import base64
import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app

_API = settings.api_url.rstrip("/")
_TENANT = "00000000-0000-0000-0000-000000000001"


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode())).decode()


_ADMIN = _cookie({"api_token": "t", "user_role": "admin", "username": "owner@example.com"})
_BOOKKEEPER = _cookie({"api_token": "t", "user_role": "bookkeeper"})


def _client(cookie: str = _ADMIN, follow: bool = False) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: cookie},
        follow_redirects=follow,
    )


def _doc_settings(**over) -> dict:
    base = {
        "tenant_id": _TENANT,
        "sender_name": "Acme Fabrication",
        "reply_to": "office@acme.example",
        "from_addresses": [],
        "default_from": {},
        "signature": "Kind regards,\n{company_name}\n{company_phone}",
        "bcc_self": False,
        "templates": {
            d: {"subject": "Quote {number} from {company_name}" if d == "quote" else "{number}",
                "body": "Hello {customer_name},\n\nQuote {number} for {title}, total {total}.\n\n{signature}",
                "customised": False}
            for d in ("quote", "invoice", "credit_note", "statement", "purchase_order", "remittance")
        },
        "document_footer": None,
        "quote_title": "Quote",
        "quote_terms": "Validity: {validity_days} days.",
        "quote_acceptance": "Send a purchase order quoting {number}.",
        "company": {"name": "Acme Fabrication", "phone": "07 4000 1111", "email": "office@acme.example"},
        "placeholders": {"customer_name": "x", "number": "y"},
        "doc_types": ["quote", "invoice"],
        "stored": {},
        "outbound_email_enabled": False,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Checklist + opening balances
# ---------------------------------------------------------------------------


def test_default_from_prefers_company_own_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from saebooks_web import comms
    from saebooks_web.comms import customer_email_status
    from saebooks_web.email_defaults import compose

    monkeypatch.setattr(settings, "customer_email_draft_mode", False)
    monkeypatch.setattr(settings, "customer_email_send_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "k")
    monkeypatch.setattr(settings, "email_platform_from", "noreply@mail.example")
    monkeypatch.setattr(
        comms, "allowed_from_addresses",
        lambda t: {"accounts@x.example", "admin@x.example", "noreply@mail.example"},
    )
    st = customer_email_status(tenant_id="t", from_addr=None, tenant_outbound_enabled=True,
                               company_email="admin@x.example")
    assert st["default_from"] == "admin@x.example"
    # An explicit per-document default still wins.
    st = customer_email_status(tenant_id="t", from_addr="accounts@x.example",
                               tenant_outbound_enabled=True, company_email="admin@x.example")
    assert st["default_from"] == "accounts@x.example"
    # Company email not on the allowlist falls back to the platform address.
    st = customer_email_status(tenant_id="t", from_addr=None, tenant_outbound_enabled=True,
                               company_email="other@x.example")
    assert st["default_from"] == "noreply@mail.example"
    out = compose("quote", {"company": {"email": "admin@x.example"}, "tenant_id": "t"}, {})
    assert out["default_from"] == "admin@x.example"


@respx.mock
async def test_checklist_links_each_step() -> None:
    respx.get(f"{_API}/api/v1/setup/checklist").mock(
        return_value=Response(200, json={
            "items": [
                {"key": "conversion_date", "label": "Conversion date", "done": False, "required": True},
                {"key": "contacts", "label": "Customers and suppliers", "done": True, "required": True, "detail": "12 contacts"},
            ],
            "done": 1, "total": 2, "required_left": 1,
        })
    )
    async with _client() as c:
        r = await c.get("/setup")
    assert r.status_code == 200
    assert 'href="/setup/opening-balances"' in r.text
    assert 'href="/admin/imports/csv/contacts"' in r.text
    assert "1 of 2 done" in r.text


@respx.mock
async def test_opening_balances_grid_preview_and_save() -> None:
    respx.get(f"{_API}/api/v1/opening-balances").mock(
        return_value=Response(200, json={"conversion_date": "2026-07-01", "opening_date": "2026-06-30", "lines": []})
    )
    respx.get(f"{_API}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [
            {"id": "a1", "code": "1-1110", "name": "Bank", "account_type": "ASSET", "is_header": False},
            {"id": "a2", "code": "3-1100", "name": "Capital", "account_type": "EQUITY", "is_header": False},
            {"id": "h1", "code": "1-1000", "name": "Current Assets", "account_type": "ASSET", "is_header": True},
        ], "total": 3})
    )
    async with _client() as c:
        r = await c.get("/setup/opening-balances")
    assert r.status_code == 200
    assert 'name="dr_a1"' in r.text and 'name="cr_a2"' in r.text
    assert "Current Assets" not in r.text  # headings are not postable

    preview = respx.post(f"{_API}/api/v1/opening-balances/preview").mock(
        return_value=Response(200, json={
            "conversion_date": "2026-07-01", "opening_date": "2026-06-30",
            "lines": [
                {"account_id": "a1", "code": "1-1110", "name": "Bank", "debit": "500.00", "credit": "0.00", "role": "posted", "is_bank": True},
                {"account_id": "a2", "code": "3-1100", "name": "Capital", "debit": "0.00", "credit": "500.00", "role": "posted", "is_bank": False},
            ],
            "total_debit": "500.00", "total_credit": "500.00", "difference": "0.00", "balanced": True,
            "control_lines": [], "journal": [], "equity_account": {"code": "3-9999", "name": "Historical Balancing", "exists": True},
            "errors": [], "ok": True,
        })
    )
    async with _client() as c:
        r = await c.post(
            "/setup/opening-balances/preview",
            data={"conversion_date": "2026-07-01", "dr_a1": "500", "cr_a2": "$500", "dr_a2": ""},
        )
    assert r.status_code == 200
    sent = _json.loads(preview.calls.last.request.content)
    assert {ln["account_id"] for ln in sent["lines"]} == {"a1", "a2"}
    assert any(ln["credit"] == "500" for ln in sent["lines"])
    assert "Save opening balances" in r.text

    save = respx.post(f"{_API}/api/v1/opening-balances").mock(
        return_value=Response(200, json={"journal_ref": "JE-000123"})
    )
    async with _client() as c:
        r = await c.post(
            "/setup/opening-balances/save",
            data={"lines_json": _json.dumps([{"account_id": "a1", "debit": "500", "credit": "0"}]), "conversion_date": "2026-07-01"},
        )
    assert r.status_code == 303 and r.headers["location"] == "/setup/opening-balances/check"
    assert _json.loads(save.calls.last.request.content)["conversion_date"] == "2026-07-01"


async def test_opening_balance_save_refused_for_non_admin() -> None:
    async with _client(_BOOKKEEPER) as c:
        r = await c.post("/setup/opening-balances/save", data={"lines_json": "[]"})
    assert r.status_code == 403


@respx.mock
async def test_cutover_check_page_shows_differences() -> None:
    respx.get(f"{_API}/api/v1/opening-balances/check").mock(
        return_value=Response(200, json={
            "conversion_date": "2026-07-01", "opening_date": "2026-06-30", "state": "differences",
            "rows": [{"account_id": "x", "code": "1-1200", "name": "Trade Debtors", "role": "receivables",
                      "entered": "3300.00", "ledger": "0.00", "difference": "-3300.00", "ties": False, "is_bank": False}],
            "unexpected": [{"account_id": "e", "code": "3-9999", "name": "Historical Balancing", "ledger": "2200.00", "is_opening_equity": True}],
            "subledgers": {"invoices": {"count": 0, "total": "0.00"}, "bills": {"count": 0, "total": "0.00"}},
            "bank_accounts": [],
        })
    )
    async with _client() as c:
        r = await c.get("/setup/opening-balances/check")
    assert r.status_code == 200
    assert "Bring across your unpaid invoices" in r.text
    assert "come back to nil" in r.text


# ---------------------------------------------------------------------------
# CSV imports
# ---------------------------------------------------------------------------


@respx.mock
async def test_contacts_import_preview_then_commit_carries_the_file() -> None:
    from conftest import TEST_CSRF_TOKEN

    csv = "Name,Type\nNew Co,Supplier\n"
    preview = respx.post(f"{_API}/api/v1/imports/csv/contacts/preview").mock(
        return_value=Response(200, json={
            "rows": [{"row": 2, "name": "New Co", "type": "SUPPLIER", "action": "create", "fills": [], "errors": []}],
            "to_create": 1, "to_update": 0, "skipped": 0, "errors": 0,
        })
    )
    async with _client() as c:
        r = await c.post(
            "/admin/imports/csv/contacts/preview",
            data={"csrf_token": TEST_CSRF_TOKEN, "default_type": "SUPPLIER"},
            files={"file": ("contacts.csv", csv.encode(), "text/csv")},
        )
    assert r.status_code == 200, r.text
    body = _json.loads(preview.calls.last.request.content)
    assert body == {"csv": csv, "options": {"default_type": "SUPPLIER"}}
    assert "Import now" in r.text
    csv_b64 = base64.b64encode(csv.encode()).decode()
    assert csv_b64 in r.text

    commit = respx.post(f"{_API}/api/v1/imports/csv/contacts/commit").mock(
        return_value=Response(200, json={"created": 1, "updated": 0, "skipped": 0, "errors": 0, "rows": []})
    )
    async with _client() as c:
        r = await c.post(
            "/admin/imports/csv/contacts/commit",
            data={"csv_b64": csv_b64, "options_json": _json.dumps({"default_type": "SUPPLIER"})},
        )
    assert r.status_code == 303 and r.headers["location"] == "/contacts"
    assert _json.loads(commit.calls.last.request.content)["csv"] == csv


@respx.mock
async def test_open_invoices_import_goes_to_opening_documents_endpoint() -> None:
    from conftest import TEST_CSRF_TOKEN

    route = respx.post(f"{_API}/api/v1/opening-balances/documents/preview").mock(
        return_value=Response(200, json={"rows": [], "to_create": 0, "skipped": 0, "errors": 0, "new_contacts": 0, "total": "0.00"})
    )
    async with _client() as c:
        r = await c.post(
            "/admin/imports/csv/open-invoices/preview",
            data={"csrf_token": TEST_CSRF_TOKEN, "create_missing": "1"},
            files={"file": ("inv.csv", b"Contact,Date,Amount due\nA,01/06/2026,5\n", "text/csv")},
        )
    assert r.status_code == 200
    sent = _json.loads(route.calls.last.request.content)
    assert sent["kind"] == "invoice" and sent["create_missing_contacts"] is True


async def test_imports_refused_for_non_admin() -> None:
    async with _client(_BOOKKEEPER) as c:
        r = await c.get("/admin/imports/csv/contacts")
    assert r.status_code == 403


@respx.mock
async def test_imports_landing_has_day_one_tiles_and_no_dead_qbo_card() -> None:
    async with _client() as c:
        r = await c.get("/admin/imports")
    assert r.status_code == 200
    for href in ("/admin/imports/csv/contacts", "/admin/imports/csv/items",
                 "/admin/imports/csv/open-invoices", "/admin/imports/csv/open-bills",
                 "/admin/imports/csv/bills", "/setup/opening-balances", "/admin/imports/xero"):
        assert f'href="{href}"' in r.text
    assert "Setup → Admin panel to access QBO" not in r.text


async def test_preview_migration_mock_now_points_at_real_page() -> None:
    async with _client() as c:
        r = await c.get("/preview/migration")
    assert r.status_code == 307 and r.headers["location"] == "/admin/imports/xero"


# ---------------------------------------------------------------------------
# Users + invitations
# ---------------------------------------------------------------------------


@respx.mock
async def test_users_page_invite_form_and_pending_list() -> None:
    respx.get(f"{_API}/api/v1/users").mock(return_value=Response(200, json={"items": [
        {"id": "u1", "username": "owner@example.com", "email": "owner@example.com", "role": "owner", "version": 1},
        {"id": "u2", "username": "bk@example.com", "email": "bk@example.com", "role": "bookkeeper", "version": 3},
        {"id": "u3", "username": "acc@example.com", "email": "acc@example.com", "role": "accountant", "version": 1,
         "invite_pending": True, "invited_at": "2026-11-13T00:00:00Z"},
    ], "total": 3}))
    respx.get(f"{_API}/api/v1/roles").mock(return_value=Response(404))
    async with _client() as c:
        r = await c.get("/settings/users")
    assert r.status_code == 200
    assert 'action="/settings/users/invite"' in r.text
    assert "Waiting to accept" in r.text and "acc@example.com" in r.text
    assert 'action="/settings/users/u2/remove"' in r.text
    assert 'action="/settings/users/u1/remove"' not in r.text  # never the owner


@respx.mock
async def test_invite_without_email_shows_copyable_link() -> None:
    route = respx.post(f"{_API}/api/v1/users/invite").mock(return_value=Response(201, json={
        "id": "u9", "email": "new@example.com", "email_sent": False,
        "detail": "Email sending is not set up on this server.",
        "accept_url": "https://books.example/accept-invite?token=abc",
    }))
    respx.get(f"{_API}/api/v1/users").mock(return_value=Response(200, json={"items": [], "total": 0}))
    respx.get(f"{_API}/api/v1/roles").mock(return_value=Response(404))
    async with _client(follow=True) as c:
        r = await c.post("/settings/users/invite", data={"email": "new@example.com", "role": "accountant"})
    assert r.status_code == 200
    assert _json.loads(route.calls.last.request.content) == {"email": "new@example.com", "role": "accountant"}
    assert "https://books.example/accept-invite?token=abc" in r.text


@respx.mock
async def test_change_role_sends_if_match() -> None:
    respx.get(f"{_API}/api/v1/users/u2").mock(return_value=Response(200, json={"id": "u2", "version": 7}))
    patch = respx.patch(f"{_API}/api/v1/users/u2").mock(return_value=Response(200, json={}))
    async with _client() as c:
        r = await c.post("/settings/users/u2/role", data={"role": "viewer"})
    assert r.status_code == 303
    assert patch.calls.last.request.headers["If-Match"] == "7"
    assert _json.loads(patch.calls.last.request.content) == {"role": "viewer", "role_id": None}


@respx.mock
async def test_cannot_remove_yourself() -> None:
    respx.get(f"{_API}/api/v1/users/u1").mock(return_value=Response(200, json={
        "id": "u1", "username": "owner@example.com", "email": "owner@example.com", "version": 1}))
    delete = respx.delete(f"{_API}/api/v1/users/u1").mock(return_value=Response(204))
    async with _client() as c:
        r = await c.post("/settings/users/u1/remove")
    assert r.status_code == 303
    assert not delete.called


@respx.mock
async def test_accept_invite_sets_password_and_signs_in() -> None:
    confirm = respx.post(f"{_API}/api/v1/auth/password-reset/confirm").mock(
        return_value=Response(200, json={"access_token": "jwt-1"})
    )
    respx.get(f"{_API}/api/v1/auth/me").mock(return_value=Response(200, json={"username": "acc@example.com", "role": "accountant"}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        page = await c.get("/accept-invite?token=tok123")
        assert page.status_code == 200 and 'name="token" value="tok123"' in page.text
        r = await c.post("/accept-invite", data={"token": "tok123", "password": "correct horse 42"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert _json.loads(confirm.calls.last.request.content) == {"token": "tok123", "new_password": "correct horse 42"}


@respx.mock
async def test_accept_invite_expired_link_says_so() -> None:
    respx.post(f"{_API}/api/v1/auth/password-reset/confirm").mock(return_value=Response(404, json={"detail": "Invalid reset token"}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/accept-invite", data={"token": "old", "password": "correct horse 42"})
    assert r.status_code == 404
    assert "expired or has already been used" in r.text


# ---------------------------------------------------------------------------
# Documents & email, numbering
# ---------------------------------------------------------------------------


@respx.mock
async def test_documents_page_explains_why_email_is_off() -> None:
    respx.get(f"{_API}/api/v1/setup/document-settings").mock(return_value=Response(200, json=_doc_settings()))
    respx.get(f"{_API}/api/v1/setup/logo").mock(return_value=Response(404))
    async with _client() as c:
        r = await c.get("/settings/documents")
    assert r.status_code == 200
    assert "Emails will not be sent yet." in r.text
    assert "has not been turned on for your account" in r.text
    assert 'name="subject_quote"' in r.text
    assert "No logo yet" in r.text


@respx.mock
async def test_documents_save_templates_and_reset() -> None:
    patch = respx.patch(f"{_API}/api/v1/setup/document-settings").mock(return_value=Response(200, json={}))
    async with _client() as c:
        r = await c.post("/settings/documents", data={
            "section": "templates",
            "subject_quote": "Estimate {number}", "body_quote": "Hi {customer_name}\r\n\r\n{signature}",
            "reset_invoice": "1",
        })
    assert r.status_code == 303
    sent = _json.loads(patch.calls.last.request.content)["templates"]
    assert sent["quote"] == {"subject": "Estimate {number}", "body": "Hi {customer_name}\n\n{signature}"}
    assert sent["invoice"] is None


@respx.mock
async def test_logo_upload_passes_base64_to_engine() -> None:
    from conftest import TEST_CSRF_TOKEN

    put = respx.put(f"{_API}/api/v1/setup/logo").mock(return_value=Response(200, json={}))
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 30
    async with _client() as c:
        r = await c.post(
            "/settings/documents/logo",
            data={"csrf_token": TEST_CSRF_TOKEN},
            files={"logo": ("logo.png", png, "image/png")},
        )
    assert r.status_code == 303
    assert base64.b64decode(_json.loads(put.calls.last.request.content)["data_b64"]) == png


@respx.mock
async def test_numbering_page_and_save() -> None:
    respx.get(f"{_API}/api/v1/setup/numbering").mock(return_value=Response(200, json={"counters": [
        {"kind": "invoice", "label": "Invoices", "prefix": "INV-", "pad_width": 6, "next_value": 1,
         "next_number": "INV-000001", "highest_issued": 0},
    ]}))
    put = respx.put(f"{_API}/api/v1/setup/numbering/invoice").mock(
        return_value=Response(200, json={"next_number": "INV-0413"})
    )
    async with _client() as c:
        page = await c.get("/settings/numbering")
        assert 'form="num-invoice"' in page.text
        r = await c.post("/settings/numbering/invoice", data={"prefix": "INV-", "next_value": "413", "pad_width": "4"})
    assert r.status_code == 303
    assert _json.loads(put.calls.last.request.content) == {"prefix": "INV-", "next_value": 413, "pad_width": 4}


# ---------------------------------------------------------------------------
# Quote email wording comes from the company, not SAE Engineering (P0-43)
# ---------------------------------------------------------------------------


@respx.mock
async def test_quote_email_composer_uses_company_settings() -> None:
    qid = "99999999-9999-9999-9999-999999999999"
    respx.get(f"{_API}/api/v1/quotes/{qid}").mock(return_value=Response(200, json={
        "id": qid, "number": "Q-000042", "title": "Shed frame", "customer_id": "c1", "currency": "AUD",
        "subtotal": "1000.00", "total": "1100.00", "issue_date": "2026-11-01", "validity_days": 30,
    }))
    respx.get(f"{_API}/api/v1/contacts/c1").mock(return_value=Response(200, json={"name": "Jo Client", "email": "jo@client.example"}))
    respx.get(f"{_API}/api/v1/setup/document-settings").mock(return_value=Response(200, json=_doc_settings()))
    async with _client() as c:
        r = await c.get(f"/quotes/{qid}/email")
    assert r.status_code == 200
    text = r.text
    for banned in ("SAE-2026", "saee.com.au", "0457 704 373", "Richard Sauer", "SAE Engineering", "kill switch"):
        assert banned not in text, banned
    assert "Quote Q-000042 from Acme Fabrication" in text
    assert "Hello Jo Client," in text and "$1,100.00" in text
    assert "07 4000 1111" in text  # signature from the company's own phone
    assert 'name="reply_to"' in text and "office@acme.example" in text


def test_compose_render_drops_empty_placeholder_lines() -> None:
    from saebooks_web.email_defaults import render, to_html

    assert render("Kind regards,\n{company_name}\n{company_phone}", {"company_name": "Acme"}) == "Kind regards,\nAcme"
    assert to_html("a <b>\n\nc") == "<p>a &lt;b&gt;</p>\n<p>c</p>"


# ---------------------------------------------------------------------------
# Comms: sender identity, platform address, status
# ---------------------------------------------------------------------------


def test_status_reasons_in_plain_words(monkeypatch: pytest.MonkeyPatch) -> None:
    from saebooks_web.comms import customer_email_status

    monkeypatch.setattr(settings, "customer_email_draft_mode", False)
    monkeypatch.setattr(settings, "customer_email_send_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "k")
    monkeypatch.setattr(settings, "email_platform_from", "noreply@mail.example")
    st = customer_email_status(tenant_id="t-new", from_addr=None, tenant_outbound_enabled=True,
                               company_addresses=["accounts@acme.example"])
    assert st["mode"] == "send"
    assert st["default_from"] == "noreply@mail.example"
    assert st["pending_addresses"] == ["accounts@acme.example"]
    st = customer_email_status(tenant_id="t-new", from_addr=None, tenant_outbound_enabled=False)
    assert st["mode"] == "off"
    assert any("not been turned on" in r for r in st["reasons"])


@respx.mock
async def test_resend_payload_carries_sender_name_and_reply_to(monkeypatch: pytest.MonkeyPatch) -> None:
    from saebooks_web.comms import run_customer_email_policy

    monkeypatch.setattr(settings, "customer_email_draft_mode", False)
    monkeypatch.setattr(settings, "customer_email_send_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "k")
    monkeypatch.setattr(settings, "email_platform_from", "noreply@mail.example")
    route = respx.post(f"{settings.resend_api_url}/emails").mock(return_value=Response(200, json={"id": "re_1"}))
    outcome, pid, _ = await run_customer_email_policy(
        tenant_id="t-new", from_addr="noreply@mail.example", to=["c@x.example"], cc=[], bcc=[],
        subject="s", body_html="<p>b</p>", body_text=None, attachments=[],
        tenant_outbound_enabled=True, reply_to="office@acme.example", from_name='Acme "Fab"\nPty',
    )
    assert outcome == "sent" and pid == "re_1"
    payload = _json.loads(route.calls.last.request.content)
    assert payload["from"] == '"Acme Fab Pty" <noreply@mail.example>'
    assert payload["reply_to"] == "office@acme.example"


# ---------------------------------------------------------------------------
# Render: company logo travels to latex-api as an asset
# ---------------------------------------------------------------------------


@respx.mock
async def test_render_passes_logo_asset_and_falls_back_without_it() -> None:
    from saebooks_web import render as render_mod

    calls: list[dict] = []

    def compile_side_effect(request):  # type: ignore[no-untyped-def]
        body = _json.loads(request.content)
        calls.append(body)
        if body.get("files"):
            return Response(422, json={"detail": "! LaTeX Error: Unknown graphics extension"})
        return Response(200, json={"pdf_url": "/files/x.pdf"})

    respx.post(f"{settings.latex_api_url}/compile").mock(side_effect=compile_side_effect)
    respx.get(f"{settings.latex_api_url}/files/x.pdf").mock(return_value=Response(200, content=b"%PDF-1.7"))
    ctx = {
        "company_logo": {"filename": "company-logo-abc.png", "content_type": "image/png", "data_b64": "aGVsbG8="},
        "document": {"title": "Estimate", "terms": ["Lead time 2 weeks."], "acceptance": "Sign below.", "footer": "Thanks!"},
        "company": {"name": "Acme", "tax_term": "GST", "bank": {}},
        "customer": {"name": "Jo"}, "lines": [], "number": "Q-1", "title": "T", "scope": "",
        "issue_date": "2026-11-01", "expiry_date": "", "validity_days": 30, "deposit_pct": "50",
        "subtotal": "1", "total": "1.1",
    }
    pdf = await render_mod.render_latex("quote", ctx)
    assert pdf.startswith(b"%PDF")
    assert calls[0]["files"] == {"company-logo-abc.png": "aGVsbG8="}
    assert "company-logo-abc.png" in calls[0]["latex"]
    assert "ESTIMATE" in calls[0]["latex"] and "Lead time 2 weeks." in calls[0]["latex"]
    assert "Thanks!" in calls[0]["latex"]
    assert "8--10 week" not in calls[0]["latex"] and "Late Fee" not in calls[0]["latex"]
    assert "files" not in calls[1] and "company-logo-abc.png" not in calls[1]["latex"]
