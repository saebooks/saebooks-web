"""Tests for the internal outbound-email service (engine #32).

Covers the contract the accounting engine's comms facades depend on, and the
PER-KIND policy scoping.

* token gate (X-Comms-Token)          → 401 when COMMS_TOKEN set + wrong/missing
* customer_doc KILL-SWITCH MATRIX (the customer policy — Resend/Graph):
    - send disabled                   → blocked
    - draft mode                      → drafted via mocked Graph
    - fully enabled                   → sent via mocked Resend
    - tenant flag false               → blocked (names the tenant flag)
    - FROM not allowlisted            → blocked (names the allowlist)
* magic_link / raw (LEGACY MAILER — SMTP, no customer kill switch):
    - sent via SMTP even with the customer kill switch fully closed
    - blocked ONLY when SMTP_HOST is empty
* magic_link renders the whitelisted template + context (engine's keys)
* attachments round-trip                → b64 in → Resend payload / MIME part
* transport failure                     → 502 on the SMTP path; 200 "failed" on
                                          the customer_doc (Resend/Graph) path
* ported SMTP + Graph transport unit tests

SMTP is mocked by patching ``comms.aiosmtplib.send``; Resend + Graph HTTP are
mocked with respx.  NO real network.
"""
from __future__ import annotations

import base64
import email
import email.policy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks_web import comms
from saebooks_web.main import app

# ---------------------------------------------------------------------------
# Mocked upstreams
# ---------------------------------------------------------------------------
_GRAPH_TENANT = "11111111-2222-3333-4444-555555555555"
_MAILBOX = "drafts-test@example.com"
_TOKEN_URL = f"https://login.microsoftonline.com/{_GRAPH_TENANT}/oauth2/v2.0/token"
_CREATE_URL = f"https://graph.microsoft.com/v1.0/users/{_MAILBOX}/messages"
_RESEND_URL = "https://api.resend.com/emails"

# The tenant + FROM that the ported allowlist accepts (from customer_email.py).
_SAUER_TENANT = "f6c01a9d-0d41-426c-aa61-e9e60e8a7995"
_ALLOWED_FROM = "admin@saee.com.au"

_FAKE_PDF = b"%PDF-1.5 fake-invoice-bytes\n%%EOF"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-closed baseline for every policy switch + transport."""
    monkeypatch.setattr(comms.settings, "comms_token", "")
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", False)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", False)
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "noreply@saee.com.au")
    monkeypatch.setattr(comms.settings, "resend_api_key", "")
    monkeypatch.setattr(comms.settings, "resend_api_url", "https://api.resend.com")


def _enable_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms.settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(comms.settings, "smtp_port", 587)
    monkeypatch.setattr(comms.settings, "smtp_user", "user")
    monkeypatch.setattr(comms.settings, "smtp_password", "pw")
    monkeypatch.setattr(comms.settings, "smtp_tls", True)


def _enable_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)
    monkeypatch.setattr(comms.settings, "resend_api_key", "re_test_key")


def _enable_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms.settings, "graph_tenant_id", _GRAPH_TENANT)
    monkeypatch.setattr(comms.settings, "graph_client_id", "client-id")
    monkeypatch.setattr(comms.settings, "graph_client_secret", "client-secret")
    monkeypatch.setattr(comms.settings, "graph_draft_mailbox", _MAILBOX)
    comms._token_cache = None


def _customer_doc_body(meta_over: dict | None = None, **over: object) -> dict:
    meta = {
        "tenant_id": _SAUER_TENANT,
        "doc_type": "invoice",
        "doc_id": "00000000-0000-0000-0000-0000000000aa",
        "doc_version": 1,
        "sent_by_user_id": None,
        "from_addr": _ALLOWED_FROM,
        "cc": [],
        "bcc": [],
        "tenant_outbound_enabled": True,
    }
    if meta_over:
        meta.update(meta_over)
    body: dict = {
        "kind": "customer_doc",
        "to": ["customer@example.com"],
        "subject": "Tax Invoice INV-042",
        "body_html": "<p>Please find your invoice attached.</p>",
        "body_text": None,
        "attachments": [
            {
                "filename": "INV-042.pdf",
                "content_b64": _b64(_FAKE_PDF),
                "content_type": "application/pdf",
            }
        ],
        "meta": meta,
    }
    body.update(over)
    return body


def _magic_link_body(**over: object) -> dict:
    body: dict = {
        "kind": "magic_link",
        "to": ["newuser@example.com"],
        "subject": "Your SAE Books Login Link",
        "body_html": None,
        "body_text": None,
        "attachments": [],
        "meta": {
            "template": "magic_link_email",
            "context": {
                "magic_link": "https://books.saee.com.au/auth/magic-link/verify/abc",
                "expires_minutes": 15,
            },
            "sender": None,
        },
    }
    body.update(over)
    return body


def _raw_body(**over: object) -> dict:
    body: dict = {
        "kind": "raw",
        "to": ["u@example.com"],
        "subject": "Verify your SAE Books email",
        "body_html": "<p>Please verify.</p>",
        "body_text": None,
        "attachments": [],
        "meta": {"sender": "noreply@saee.com.au"},
    }
    body.update(over)
    return body


async def _post(body: dict, headers: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/internal/comms/send", json=body, headers=headers or {})


# ===========================================================================
# Token gate
# ===========================================================================


@pytest.mark.asyncio
async def test_token_gate_missing_and_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setattr(comms.settings, "comms_token", "s3cret-comms")
    r = await _post(_customer_doc_body())
    assert r.status_code == 401, r.text
    r2 = await _post(_customer_doc_body(), headers={"X-Comms-Token": "wrong"})
    assert r2.status_code == 401, r2.text


@pytest.mark.asyncio
async def test_token_absent_allows_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_token_correct_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setattr(comms.settings, "comms_token", "s3cret-comms")
    r = await _post(_customer_doc_body(), headers={"X-Comms-Token": "s3cret-comms"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "blocked"


# ===========================================================================
# customer_doc — kill-switch matrix (Resend / Graph)
# ===========================================================================


@pytest.mark.asyncio
async def test_customer_doc_send_disabled_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """send off + draft off → BLOCKED naming the env key."""
    _reset(monkeypatch)
    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert body["provider_id"] is None
    assert "SAEBOOKS_EMAIL_SEND_ENABLED" in body["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_customer_doc_draft_mode_drafts(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """draft mode ON → DRAFTED via mocked Graph; no Resend call."""
    _reset(monkeypatch)
    _enable_graph(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)

    respx_mock.post(_TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    create_route = respx_mock.post(_CREATE_URL).mock(
        return_value=Response(201, json={"id": "AAMkADraftId", "webLink": "https://o/x"})
    )
    resend_route = respx_mock.post(_RESEND_URL).mock(
        return_value=Response(200, json={"id": "nope"})
    )

    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "drafted"
    assert body["provider_id"] == "AAMkADraftId"
    assert not resend_route.called  # draft mode never sends via Resend

    payload = json.loads(create_route.calls.last.request.content)
    assert payload["subject"] == "Tax Invoice INV-042"
    assert base64.b64decode(payload["attachments"][0]["contentBytes"]) == _FAKE_PDF


@pytest.mark.asyncio
@respx.mock
async def test_customer_doc_fully_enabled_sends_via_resend(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """send ON + tenant flag ON + FROM allowlisted + key set → SENT via Resend."""
    _reset(monkeypatch)
    _enable_resend(monkeypatch)

    resend_route = respx_mock.post(_RESEND_URL).mock(
        return_value=Response(200, json={"id": "re_abc123"})
    )

    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "sent"
    assert body["provider_id"] == "re_abc123"
    assert "Resend" in body["detail"]

    payload = json.loads(resend_route.calls.last.request.content)
    assert payload["from"] == _ALLOWED_FROM
    assert payload["to"] == ["customer@example.com"]
    assert payload["subject"] == "Tax Invoice INV-042"
    assert base64.b64decode(payload["attachments"][0]["content"]) == _FAKE_PDF


@pytest.mark.asyncio
async def test_customer_doc_tenant_flag_false_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """send ON + FROM allowlisted, but per-tenant flag false → BLOCKED."""
    _reset(monkeypatch)
    _enable_resend(monkeypatch)
    r = await _post(_customer_doc_body(meta_over={"tenant_outbound_enabled": False}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert "tenants.outbound_email_enabled" in body["detail"]


@pytest.mark.asyncio
async def test_customer_doc_from_not_allowlisted_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """send ON + tenant flag ON, but FROM not in the allowlist → BLOCKED."""
    _reset(monkeypatch)
    _enable_resend(monkeypatch)
    r = await _post(_customer_doc_body(meta_over={"from_addr": "evil@example.com"}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert "not in tenant allowlist" in body["detail"]


@pytest.mark.asyncio
async def test_customer_doc_empty_resend_key_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """All gates pass but RESEND_API_KEY empty → BLOCKED (as in the original)."""
    _reset(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)
    # resend_api_key stays "" from _reset.
    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert "RESEND_API_KEY is empty" in body["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_customer_doc_resend_failure_is_failed_not_502(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Resend non-2xx → 200 outcome 'failed' (mirrors the original's mode)."""
    _reset(monkeypatch)
    _enable_resend(monkeypatch)
    respx_mock.post(_RESEND_URL).mock(return_value=Response(422, text="bad from"))

    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "failed"
    assert body["provider_id"] is None
    assert "Resend 422" in body["detail"]


# ===========================================================================
# magic_link / raw — LEGACY MAILER (SMTP, no customer kill switch)
# ===========================================================================


@pytest.mark.asyncio
async def test_magic_link_sends_via_smtp_despite_customer_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer kill switch fully closed (send off, DRAFT MODE on) — magic_link
    STILL delivers via SMTP.  Login links must keep working."""
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)
    _enable_graph(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)  # customer-only
    # send flag stays OFF.

    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)

    r = await _post(_magic_link_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "sent"
    assert "SMTP" in body["detail"]
    sent.assert_awaited_once()  # SMTP, NOT drafted


@pytest.mark.asyncio
async def test_raw_sends_via_smtp_despite_customer_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)

    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)

    r = await _post(_raw_body())
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sent"
    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["hostname"] == "mail.example.com"
    assert kwargs["recipients"] == ["u@example.com"]


@pytest.mark.asyncio
async def test_magic_link_blocked_only_on_empty_smtp_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SMTP_HOST → blocked 'SMTP_HOST is empty' (the ONLY block for these kinds)."""
    _reset(monkeypatch)  # smtp_host stays ""
    r = await _post(_magic_link_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert "SMTP_HOST" in body["detail"]


@pytest.mark.asyncio
async def test_billing_receipt_uses_legacy_mailer(monkeypatch: pytest.MonkeyPatch) -> None:
    """billing_receipt is a non-customer kind → SMTP path, not the kill switch."""
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)
    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)
    r = await _post(_raw_body(kind="billing_receipt"))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sent"
    sent.assert_awaited_once()


# ===========================================================================
# magic_link template rendering (engine's meta.template + meta.context)
# ===========================================================================


@pytest.mark.asyncio
async def test_magic_link_renders_template_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whitelisted template renders from meta.context (magic_link + TTL)."""
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)

    captured: dict = {}

    async def _capture(msg, *_a, **_kw):
        captured["msg"] = msg

    monkeypatch.setattr(comms.aiosmtplib, "send", _capture)

    link = "https://books.saee.com.au/auth/magic-link/verify/xyz789"
    r = await _post(
        _magic_link_body(
            meta={
                "template": "magic_link_email",
                "context": {"magic_link": link, "expires_minutes": 20},
                "sender": None,
            }
        )
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sent"

    html_parts = [
        p.get_payload(decode=True).decode()
        for p in captured["msg"].walk()
        if p.get_content_type() == "text/html"
    ]
    assert any(link in h for h in html_parts)
    assert any("20 minutes" in h for h in html_parts)


@pytest.mark.asyncio
async def test_magic_link_unknown_template_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)
    r = await _post(
        _magic_link_body(meta={"template": "../../etc/passwd", "context": {}})
    )
    assert r.status_code == 400, r.text
    assert "template" in r.json()["detail"]


def test_render_email_template_unit() -> None:
    html = comms.render_email_template(
        "magic_link_email", {"magic_link": "https://x/abc", "expires_minutes": 15}
    )
    assert "https://x/abc" in html
    assert "15 minutes" in html
    assert "SAE Books" in html


# ===========================================================================
# Attachments round-trip (SMTP MIME) + bad base64
# ===========================================================================


@pytest.mark.asyncio
async def test_attachment_roundtrips_into_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw-kind base64 attachment reaches the wire as a decoded MIME part."""
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)

    captured: dict = {}

    async def _capture(msg, *_a, **_kw):
        captured["msg"] = msg

    monkeypatch.setattr(comms.aiosmtplib, "send", _capture)

    body = _raw_body(
        attachments=[
            {
                "filename": "r.pdf",
                "content_b64": _b64(_FAKE_PDF),
                "content_type": "application/pdf",
            }
        ]
    )
    r = await _post(body)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sent"

    payloads = [
        p.get_payload(decode=True)
        for p in captured["msg"].walk()
        if p.get_filename() == "r.pdf"
    ]
    assert payloads == [_FAKE_PDF]


@pytest.mark.asyncio
async def test_bad_base64_attachment_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    body = _raw_body(
        attachments=[{"filename": "x.pdf", "content_b64": "!!!not-base64!!!"}]
    )
    r = await _post(body)
    assert r.status_code == 400, r.text
    assert "base64" in r.json()["detail"]


# ===========================================================================
# Transport failure → 502 (SMTP path only)
# ===========================================================================


@pytest.mark.asyncio
async def test_smtp_failure_maps_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SMTP delivery error on a non-customer kind → HTTP 502."""
    _reset(monkeypatch)
    _enable_smtp(monkeypatch)

    async def _boom(*_a, **_kw):
        raise OSError("Connection refused")

    monkeypatch.setattr(comms.aiosmtplib, "send", _boom)

    r = await _post(_raw_body())
    assert r.status_code == 502, r.text
    assert "SMTP delivery failed" in r.json()["detail"]


# ===========================================================================
# Request validation
# ===========================================================================


@pytest.mark.asyncio
async def test_invalid_kind_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    r = await _post(_raw_body(kind="spam"))
    assert r.status_code == 400, r.text
    assert "kind" in r.json()["detail"]


@pytest.mark.asyncio
async def test_missing_recipient_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    r = await _post(_raw_body(to=[]))
    assert r.status_code == 400, r.text
    assert "recipient" in r.json()["detail"]


@pytest.mark.asyncio
async def test_empty_body_html_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset(monkeypatch)
    r = await _post(_raw_body(body_html="   "))
    assert r.status_code == 400, r.text
    assert "body_html" in r.json()["detail"]


# ===========================================================================
# Ported SMTP transport unit tests (from engine tests/services/test_mailer.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_smtp_outbox_mode_writes_eml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "books@sauer.com.au")
    monkeypatch.setattr(comms.settings, "mail_outbox_dir", str(tmp_path))
    result = await comms.send_smtp(
        to=["acme@example.com"],
        subject="Your invoice INV-000001",
        html="<p>Hello <b>world</b></p>",
    )
    assert result.mode == "outbox"
    msg = email.message_from_bytes(Path(result.outbox_path or "").read_bytes())
    assert msg["To"] == "acme@example.com"
    assert msg["From"] == "books@sauer.com.au"
    parts = {p.get_content_type() for p in msg.walk()}
    assert "text/html" in parts and "text/plain" in parts


@pytest.mark.asyncio
async def test_smtp_empty_recipient_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "books@sauer.com.au")
    with pytest.raises(comms.EmailError, match="No recipients"):
        await comms.send_smtp(to=[], subject="t", html="<p>x</p>")


@pytest.mark.asyncio
async def test_smtp_empty_sender_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "")
    monkeypatch.setattr(comms.settings, "mail_outbox_dir", str(tmp_path))
    with pytest.raises(comms.EmailError, match="No sender"):
        await comms.send_smtp(to=["a@example.com"], subject="t", html="<p>x</p>")


@pytest.mark.asyncio
async def test_smtp_explicit_text_part_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "books@sauer.com.au")
    monkeypatch.setattr(comms.settings, "mail_outbox_dir", str(tmp_path))
    result = await comms.send_smtp(
        to=["a@example.com"], subject="t", html="<p>ignore me</p>", text="HELLO CUSTOM TEXT"
    )
    msg = email.message_from_bytes(
        Path(result.outbox_path or "").read_bytes(), policy=email.policy.default
    )
    text_parts = [
        p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain"
    ]
    assert any("HELLO CUSTOM TEXT" in t for t in text_parts)


# ===========================================================================
# Ported Graph draft unit tests
# ===========================================================================


@pytest.mark.asyncio
@respx.mock
async def test_create_outlook_draft_unit(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(monkeypatch)
    respx_mock.post(_TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx_mock.post(_CREATE_URL).mock(
        return_value=Response(201, json={"id": "DraftXYZ", "webLink": "https://o/x"})
    )
    res = await comms.create_outlook_draft(
        mailbox=_MAILBOX,
        subject="s",
        to=["c@example.com"],
        cc=[],
        bcc=[],
        body_html="<p>hi</p>",
        attachments=[comms.Attachment("f.pdf", _FAKE_PDF, "application/pdf")],
    )
    assert res.draft_id == "DraftXYZ"
    assert res.error is None


@pytest.mark.asyncio
async def test_create_outlook_draft_no_mailbox() -> None:
    res = await comms.create_outlook_draft(
        mailbox="", subject="s", to=["c@example.com"], cc=[], bcc=[],
        body_html="<p>hi</p>", attachments=[],
    )
    assert res.draft_id is None
    assert "not configured" in (res.error or "")


@pytest.mark.asyncio
async def test_customer_doc_draft_fails_closed_without_graph_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """draft mode ON but Graph unconfigured → 200 'failed' (never sends)."""
    _reset(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)
    monkeypatch.setattr(comms.settings, "graph_draft_mailbox", "")
    comms._token_cache = None
    r = await _post(_customer_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "failed"
    assert "not configured" in body["detail"]
