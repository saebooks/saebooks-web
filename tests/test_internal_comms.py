"""Tests for the internal outbound-email service (engine #32).

Covers the contract the accounting engine's comms facades depend on, and the
SACRED two-key kill switch ported from the engine's customer_email policy.

* token gate (X-Comms-Token)          → 401 when COMMS_TOKEN set + wrong/missing
* KILL-SWITCH MATRIX (the critical cells):
    - send disabled                   → 200 {"outcome": "blocked"}
    - draft mode                      → 200 {"outcome": "drafted"} via mocked Graph
    - fully enabled                   → 200 {"outcome": "sent"} via mocked SMTP
    - enabled but SMTP_HOST empty      → 200 {"outcome": "blocked"} (never false-sent)
* attachments round-trip              → b64 in → MIME part out (SMTP) / Graph payload
* magic_link kind                     → renders the ported template with meta fields
* transport failure                   → 502 (SMTP delivery error; Graph draft failure)
* ported SMTP transport unit tests    → outbox mode, guards, aiosmtplib call
* ported Graph draft unit tests       → token + create; fail-closed without config

SMTP is mocked by patching ``comms.aiosmtplib.send``; Graph HTTP is mocked with
respx.  NO real network.
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
# Graph endpoints (mocked) — must match outlook_drafts URLs
# ---------------------------------------------------------------------------
_GRAPH_TENANT = "11111111-2222-3333-4444-555555555555"
_MAILBOX = "drafts-test@example.com"
_TOKEN_URL = f"https://login.microsoftonline.com/{_GRAPH_TENANT}/oauth2/v2.0/token"
_CREATE_URL = f"https://graph.microsoft.com/v1.0/users/{_MAILBOX}/messages"

_FAKE_PDF = b"%PDF-1.5 fake-invoice-bytes\n%%EOF"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _reset_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-closed baseline: no token, send off, draft off, no SMTP host."""
    monkeypatch.setattr(comms.settings, "comms_token", "")
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", False)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", False)
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "admin@saee.com.au")


def _enable_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms.settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(comms.settings, "smtp_port", 587)
    monkeypatch.setattr(comms.settings, "smtp_user", "user")
    monkeypatch.setattr(comms.settings, "smtp_password", "pw")
    monkeypatch.setattr(comms.settings, "smtp_tls", True)


def _enable_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms.settings, "graph_tenant_id", _GRAPH_TENANT)
    monkeypatch.setattr(comms.settings, "graph_client_id", "client-id")
    monkeypatch.setattr(comms.settings, "graph_client_secret", "client-secret")
    monkeypatch.setattr(comms.settings, "graph_draft_mailbox", _MAILBOX)
    comms._token_cache = None  # reset module-level Graph token cache


def _doc_body(**over: object) -> dict:
    body = {
        "kind": "customer_doc",
        "to": ["customer@example.com"],
        "subject": "Tax Invoice INV-042",
        "body_html": "<p>Please find your invoice attached.</p>",
        "attachments": [
            {
                "filename": "INV-042.pdf",
                "content_b64": _b64(_FAKE_PDF),
                "content_type": "application/pdf",
            }
        ],
        "meta": {"from": "admin@saee.com.au"},
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
    """When COMMS_TOKEN is set, a missing or wrong X-Comms-Token → 401."""
    _reset_kill_switch(monkeypatch)
    monkeypatch.setattr(comms.settings, "comms_token", "s3cret-comms")

    r = await _post(_doc_body())
    assert r.status_code == 401, r.text
    r2 = await _post(_doc_body(), headers={"X-Comms-Token": "wrong"})
    assert r2.status_code == 401, r2.text


@pytest.mark.asyncio
async def test_token_absent_allows_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty COMMS_TOKEN (default) → endpoint open; reaches the policy (blocked)."""
    _reset_kill_switch(monkeypatch)
    r = await _post(_doc_body())
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_token_correct_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correct X-Comms-Token → past the gate (constant-time compare)."""
    _reset_kill_switch(monkeypatch)
    monkeypatch.setattr(comms.settings, "comms_token", "s3cret-comms")
    r = await _post(_doc_body(), headers={"X-Comms-Token": "s3cret-comms"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "blocked"


# ===========================================================================
# KILL-SWITCH MATRIX — the critical cells
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_switch_send_disabled_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """send off + draft off → BLOCKED (nothing leaves the box)."""
    _reset_kill_switch(monkeypatch)
    _enable_smtp(monkeypatch)  # SMTP configured, but send flag is OFF
    # Guard: if the policy tried to send, this mock would record a call.
    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)

    r = await _post(_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert body["provider_id"] is None
    assert "SAEBOOKS_EMAIL_SEND_ENABLED" in body["detail"]
    sent.assert_not_awaited()  # NOTHING sent


@pytest.mark.asyncio
@respx.mock
async def test_kill_switch_draft_mode_drafts(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """draft mode ON → DRAFTED via mocked Graph (send flag irrelevant)."""
    _reset_kill_switch(monkeypatch)
    _enable_graph(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)
    # Send flag deliberately left OFF — draft mode overrides it.
    # Guard: no SMTP send may happen in draft mode.
    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)

    respx_mock.post(_TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    create_route = respx_mock.post(_CREATE_URL).mock(
        return_value=Response(
            201, json={"id": "AAMkADraftId", "webLink": "https://outlook/x"}
        )
    )

    r = await _post(_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "drafted"
    assert body["provider_id"] == "AAMkADraftId"
    assert _MAILBOX in body["detail"]
    sent.assert_not_awaited()

    # The draft payload carried the subject, recipient and attachment.
    assert create_route.called
    payload = json.loads(create_route.calls.last.request.content)
    assert payload["subject"] == "Tax Invoice INV-042"
    assert payload["toRecipients"][0]["emailAddress"]["address"] == "customer@example.com"
    assert payload["attachments"][0]["name"] == "INV-042.pdf"
    assert base64.b64decode(payload["attachments"][0]["contentBytes"]) == _FAKE_PDF


@pytest.mark.asyncio
async def test_kill_switch_fully_enabled_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """send ON + draft OFF + SMTP configured → SENT via mocked SMTP."""
    _reset_kill_switch(monkeypatch)
    _enable_smtp(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)

    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)

    r = await _post(_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "sent"
    assert body["provider_id"]  # a Message-ID string
    assert "SMTP" in body["detail"]

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["hostname"] == "mail.example.com"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "user"
    assert kwargs["password"] == "pw"
    assert kwargs["start_tls"] is True
    assert kwargs["recipients"] == ["customer@example.com"]


@pytest.mark.asyncio
async def test_enabled_but_no_smtp_host_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """send ON but SMTP_HOST empty → BLOCKED, never a false 'sent'/dev-outbox."""
    _reset_kill_switch(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)
    # smtp_host stays "" from _reset_kill_switch.
    sent = AsyncMock()
    monkeypatch.setattr(comms.aiosmtplib, "send", sent)

    r = await _post(_doc_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "blocked"
    assert "SMTP_HOST" in body["detail"]
    sent.assert_not_awaited()


# ===========================================================================
# Attachments round-trip (b64 → MIME part on the SMTP path)
# ===========================================================================


@pytest.mark.asyncio
async def test_attachment_roundtrips_into_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base64 attachment reaches the wire as a decoded MIME part."""
    _reset_kill_switch(monkeypatch)
    _enable_smtp(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)

    captured: dict = {}

    async def _capture(msg, *_a, **_kw):
        captured["msg"] = msg

    monkeypatch.setattr(comms.aiosmtplib, "send", _capture)

    r = await _post(_doc_body())
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sent"

    msg = captured["msg"]
    payloads = [
        p.get_payload(decode=True)
        for p in msg.walk()
        if p.get_filename() == "INV-042.pdf"
    ]
    assert payloads == [_FAKE_PDF]
    # And the HTML + text alternatives both exist.
    types = {p.get_content_type() for p in msg.walk()}
    assert "text/html" in types
    assert "text/plain" in types


@pytest.mark.asyncio
async def test_bad_base64_attachment_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-decodable attachment → 400 before any transport."""
    _reset_kill_switch(monkeypatch)
    body = _doc_body(
        attachments=[{"filename": "x.pdf", "content_b64": "!!!not-base64!!!"}]
    )
    r = await _post(body)
    assert r.status_code == 400, r.text
    assert "base64" in r.json()["detail"]


# ===========================================================================
# magic_link kind — renders the ported template with meta fields
# ===========================================================================


@pytest.mark.asyncio
async def test_magic_link_renders_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """magic_link kind renders the template from meta.magic_link + expires."""
    _reset_kill_switch(monkeypatch)
    _enable_smtp(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)

    captured: dict = {}

    async def _capture(msg, *_a, **_kw):
        captured["msg"] = msg

    monkeypatch.setattr(comms.aiosmtplib, "send", _capture)

    link = "https://books.saee.com.au/auth/magic?token=abc123"
    r = await _post(
        {
            "kind": "magic_link",
            "to": "newuser@example.com",
            "meta": {"magic_link": link, "expires_minutes": 20},
        }
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sent"

    msg = captured["msg"]
    assert msg["Subject"] == "Your SAE Books Login Link"  # defaulted
    html_parts = [
        p.get_payload(decode=True).decode()
        for p in msg.walk()
        if p.get_content_type() == "text/html"
    ]
    assert any(link in h for h in html_parts)
    assert any("20 minutes" in h for h in html_parts)


@pytest.mark.asyncio
async def test_magic_link_requires_meta_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """magic_link kind without meta.magic_link → 400."""
    _reset_kill_switch(monkeypatch)
    r = await _post({"kind": "magic_link", "to": "u@example.com", "meta": {}})
    assert r.status_code == 400, r.text
    assert "magic_link" in r.json()["detail"]


def test_render_magic_link_unit() -> None:
    """render_magic_link injects the URL and TTL into the ported template."""
    html = comms.render_magic_link(
        magic_link="https://x.example/abc", expires_minutes=15
    )
    assert "https://x.example/abc" in html
    assert "15 minutes" in html
    assert "SAE Books" in html


# ===========================================================================
# Transport failure → 502
# ===========================================================================


@pytest.mark.asyncio
async def test_smtp_failure_maps_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SMTP delivery error surfaces as HTTP 502."""
    _reset_kill_switch(monkeypatch)
    _enable_smtp(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_send_enabled", True)

    async def _boom(*_a, **_kw):
        raise OSError("Connection refused")

    monkeypatch.setattr(comms.aiosmtplib, "send", _boom)

    r = await _post(_doc_body())
    assert r.status_code == 502, r.text
    assert "SMTP delivery failed" in r.json()["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_draft_failure_maps_to_502(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Graph draft-creation failure (in draft mode) → HTTP 502."""
    _reset_kill_switch(monkeypatch)
    _enable_graph(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)

    respx_mock.post(_TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx_mock.post(_CREATE_URL).mock(
        return_value=Response(500, text="graph exploded")
    )

    r = await _post(_doc_body())
    assert r.status_code == 502, r.text
    assert "draft mode" in r.json()["detail"]


@pytest.mark.asyncio
async def test_draft_mode_fails_closed_without_graph_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """draft mode ON but Graph unconfigured → 502 (fail closed, never sends)."""
    _reset_kill_switch(monkeypatch)
    monkeypatch.setattr(comms.settings, "customer_email_draft_mode", True)
    monkeypatch.setattr(comms.settings, "graph_draft_mailbox", "")
    comms._token_cache = None

    r = await _post(_doc_body())
    assert r.status_code == 502, r.text
    assert "not configured" in r.json()["detail"]


# ===========================================================================
# Request validation
# ===========================================================================


@pytest.mark.asyncio
async def test_invalid_kind_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_kill_switch(monkeypatch)
    r = await _post(_doc_body(kind="spam"))
    assert r.status_code == 400, r.text
    assert "kind" in r.json()["detail"]


@pytest.mark.asyncio
async def test_missing_recipient_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_kill_switch(monkeypatch)
    r = await _post(_doc_body(to=[]))
    assert r.status_code == 400, r.text
    assert "recipient" in r.json()["detail"]


@pytest.mark.asyncio
async def test_empty_body_html_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_kill_switch(monkeypatch)
    r = await _post(_doc_body(body_html="   "))
    assert r.status_code == 400, r.text
    assert "body_html" in r.json()["detail"]


# ===========================================================================
# Ported SMTP transport unit tests (from engine tests/services/test_mailer.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_smtp_outbox_mode_writes_eml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty smtp_host → the transport writes a valid .eml to the outbox."""
    monkeypatch.setattr(comms.settings, "smtp_host", "")
    monkeypatch.setattr(comms.settings, "smtp_from", "books@sauer.com.au")
    monkeypatch.setattr(comms.settings, "mail_outbox_dir", str(tmp_path))

    result = await comms.send_smtp(
        to=["acme@example.com"],
        subject="Your invoice INV-000001",
        html="<p>Hello <b>world</b></p>",
    )
    assert result.mode == "outbox"
    assert result.outbox_path is not None
    eml = Path(result.outbox_path)
    assert eml.exists() and eml.suffix == ".eml"
    msg = email.message_from_bytes(eml.read_bytes())
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
        to=["a@example.com"],
        subject="t",
        html="<p>ignore me</p>",
        text="HELLO CUSTOM TEXT",
    )
    msg = email.message_from_bytes(
        Path(result.outbox_path or "").read_bytes(), policy=email.policy.default
    )
    text_parts = [
        p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain"
    ]
    assert any("HELLO CUSTOM TEXT" in t for t in text_parts)


# ===========================================================================
# Ported Graph draft unit tests (from engine test_customer_email_draft_mode.py)
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
async def test_create_outlook_draft_no_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    res = await comms.create_outlook_draft(
        mailbox="",
        subject="s",
        to=["c@example.com"],
        cc=[],
        bcc=[],
        body_html="<p>hi</p>",
        attachments=[],
    )
    assert res.draft_id is None
    assert "not configured" in (res.error or "")
