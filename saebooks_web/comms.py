"""Internal outbound-email service — the web app owns comms policy + transport.

Two-repo extraction (Gitea saebooks/saebooks #31/#32): the accounting engine
is the *accountant* (it produces facts — recipients, subject, assembled HTML,
attachment bytes, the per-tenant outbound flag, and records the audit rows)
and this app module is the *bookkeeper* — it owns POLICY (send/draft/block)
and TRANSPORT.  The engine reaches it over HTTP via
``saebooks/services/comms_client.py``; the two engine facades
(``services.customer_email`` and ``services.email``) are the fixed contract
this module conforms to.

Ported, behaviourally verbatim from the engine's pre-#32 code:

* the customer-email POLICY — the two-key kill switch + per-tenant FROM
  allowlist + Outlook draft mode, from the ORIGINAL
  ``saebooks/services/customer_email.py`` (git d3a052e);
* the Resend API transport (customer_doc "sent" path);
* the SMTP transport from ``saebooks/services/mailer.py`` (non-customer send);
* the Microsoft Graph draft transport from
  ``saebooks/services/outlook_drafts.py`` (customer_doc draft mode);
* the Jinja email templates (``templates/emails/``).

PER-KIND POLICY SCOPING (the critical rule)
-------------------------------------------
The customer kill switch is scoped to customer documents ONLY:

* ``kind == "customer_doc"`` → the FULL customer policy: draft mode (Graph),
  the two-key kill switch (``SAEBOOKS_EMAIL_SEND_ENABLED`` env AND the
  per-tenant ``tenant_outbound_enabled`` fact passed in ``meta``), the
  per-tenant FROM allowlist, and the Resend send.  Default-closed: an
  un-configured deployment BLOCKS.

* ``kind in {"magic_link", "raw", "billing_receipt"}`` → LEGACY MAILER
  semantics: send via SMTP whenever ``SMTP_HOST`` is configured; ``blocked``
  ("SMTP_HOST is empty") when it is not.  These are NEVER drafted and are
  NEVER gated by the customer kill switch — login links and receipts must keep
  delivering even while customer email is in draft mode.

Public surface
--------------
``POST /internal/comms/send`` — the server-to-server endpoint the engine's
comms facades call.  Contract (do not deviate — the engine depends on it):

* If ``COMMS_TOKEN`` is set, header ``X-Comms-Token`` must match it
  (constant-time), else 401.  Empty token → dev mode, open.
* Body (JSON object)::

      {
        "kind": "customer_doc" | "magic_link" | "billing_receipt" | "raw",
        "to": ["a@x", ...],                       # always a list
        "subject": "...",
        "body_html": "<p>...</p>" | null,          # null for magic_link
        "body_text": "..." | null,
        "attachments": [
          {"filename": "...", "content_b64": "...", "content_type": "..."}
        ],
        "meta": { ... kind-specific ... }
      }

  ``meta`` per kind (matches the engine facades exactly):
    - customer_doc: ``{tenant_id, doc_type, doc_id, doc_version,
      sent_by_user_id, from_addr, cc:[], bcc:[], tenant_outbound_enabled}``
    - magic_link:   ``{template, context:{}, sender}`` — body_html null; the
      module renders the whitelisted Jinja ``template`` name with ``context``.
    - raw / billing_receipt: ``{sender}`` — body_html pre-assembled.
* 200 → ``{"outcome": "sent"|"drafted"|"blocked"|"failed", "provider_id":
  str|None, "detail": str|None}``.  customer_doc: ``sent`` via Resend,
  ``drafted`` via Graph, ``blocked`` by the kill switch/allowlist/tenant flag,
  ``failed`` on a Resend/Graph transport error (mirrors the original, which
  returned mode='failed' rather than raising).  non-customer: ``sent`` via
  SMTP, ``blocked`` on empty ``SMTP_HOST``.
* 400 → malformed request (bad kind / no recipient / empty subject-or-body /
  unknown magic_link template / bad attachment base64).
* 502 → ``{"detail": ...}`` transport failure on the SMTP (non-customer) path
  only (the old mailer raised ``EmailError`` on delivery failure; the engine's
  ``email`` facade re-raises it).  customer_doc never returns 502 — its
  transport failures are 200 ``failed``.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
import httpx
import jinja2
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from saebooks_web.brand import current_brand
from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.comms")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    """A decoded attachment: raw bytes + filename + MIME content-type."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class TransportError(RuntimeError):
    """A transport was attempted and failed (SMTP delivery / Graph draft).

    Distinct from a policy *block* — a block is a deliberate, successful
    refusal (200 ``blocked``); a TransportError is an infrastructure failure
    surfaced to the caller as HTTP 502.
    """


# ---------------------------------------------------------------------------
# SMTP transport  (ported from saebooks/services/mailer.py)
# ---------------------------------------------------------------------------
#
# Empty ``settings.smtp_host`` flips the transport into outbox mode — every
# call writes an RFC 5322 .eml into ``settings.mail_outbox_dir`` instead of
# hitting the wire (dev convenience).  The send POLICY below never routes to
# this transport with an empty host (it blocks first), so the outbox branch is
# a pure dev/unit-test path.


class EmailError(RuntimeError):
    """Raised when an email cannot be delivered or stored by the transport."""


@dataclass(frozen=True)
class EmailResult:
    """What the SMTP transport returns — lets callers/tests assert the path."""

    mode: str  # "smtp" | "outbox"
    message_id: str
    outbox_path: str | None = None
    recipients: tuple[str, ...] = field(default_factory=tuple)


def _html_to_text(html: str) -> str:
    """Crude HTML → plain-text fallback for the text/plain alternative.

    Not marketing-quality — strips tags, collapses whitespace.  A caller that
    cares about the text part passes ``text=`` explicitly.
    """
    no_tags = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", no_tags)).strip()


def _slug(value: str) -> str:
    """Filesystem-safe slug for outbox filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "msg"


def _build_message(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    html: str,
    text: str | None,
    attachments: list[Attachment] | None,
) -> EmailMessage:
    """Construct a multipart/alternative EmailMessage with attachments.

    ``cc`` is added as a header (bcc is never headered — it is passed to the
    transport as an explicit recipient so it stays hidden).
    """
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(text or _html_to_text(html))
    msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        maintype, _, subtype = att.content_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            att.content,
            maintype=maintype,
            subtype=subtype,
            filename=att.filename,
        )
    return msg


def _write_outbox(dir_path: str, msg: EmailMessage, subject: str) -> str:
    outbox = Path(dir_path)
    try:
        outbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EmailError(f"Cannot create mail outbox at {dir_path!r}: {exc}") from exc

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    path = outbox / f"{ts}-{_slug(subject)}.eml"
    try:
        path.write_bytes(bytes(msg))
    except OSError as exc:
        raise EmailError(f"Cannot write outbox file {path}: {exc}") from exc
    return str(path)


async def send_smtp(
    *,
    to: list[str],
    subject: str,
    html: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    text: str | None = None,
    attachments: list[Attachment] | None = None,
    sender: str | None = None,
) -> EmailResult:
    """Deliver ``html`` to ``to`` (+ cc/bcc) over SMTP, or write to the outbox.

    When ``settings.smtp_host`` is empty the message is written to
    ``settings.mail_outbox_dir`` as an .eml.  Otherwise an SMTP session is
    opened against ``settings.smtp_host:settings.smtp_port`` with STARTTLS
    when ``settings.smtp_tls`` is true.  Raises ``EmailError`` on delivery /
    filesystem failure.
    """
    cc = cc or []
    bcc = bcc or []
    recipients = list(to)
    if not recipients:
        raise EmailError("No recipients supplied")

    sender_addr = sender or settings.smtp_from
    if not sender_addr:
        raise EmailError("No sender configured (settings.smtp_from is empty)")

    msg = _build_message(
        sender=sender_addr,
        to=to,
        cc=cc,
        subject=subject,
        html=html,
        text=text,
        attachments=attachments,
    )
    # message-id header — EmailMessage won't set it for us.
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    msg_id = f"<{ts}.{_slug(subject)}@saebooks>"
    msg["Message-ID"] = msg_id

    if not settings.smtp_host:
        outbox_path = _write_outbox(settings.mail_outbox_dir, msg, subject)
        logger.info("comms: email written to outbox at %s (to=%d)", outbox_path, len(recipients))
        return EmailResult(
            mode="outbox",
            message_id=msg_id,
            outbox_path=outbox_path,
            recipients=tuple(recipients),
        )

    # bcc recipients are delivered but never headered — pass the full envelope
    # recipient list to the transport explicitly.
    all_recipients = list(to) + list(cc) + list(bcc)
    try:
        await aiosmtplib.send(
            msg,
            sender=sender_addr,
            recipients=all_recipients,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_tls,
        )
    except Exception as exc:
        raise EmailError(f"SMTP delivery failed: {exc}") from exc

    logger.info("comms: email sent via SMTP host=%s to=%d", settings.smtp_host, len(all_recipients))
    return EmailResult(mode="smtp", message_id=msg_id, recipients=tuple(all_recipients))


# ---------------------------------------------------------------------------
# Microsoft Graph draft transport  (ported from outlook_drafts.py)
# ---------------------------------------------------------------------------
#
# Application-permission (client_credentials) flow.  This NEVER sends mail —
# it only creates drafts (POST /users/{mailbox}/messages), so it needs only
# Mail.ReadWrite, not Mail.Send.  ``create_outlook_draft`` never raises: it
# returns ``DraftResult.error`` so the policy can surface the failure.

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE = "https://login.microsoftonline.com"

# Module-level token cache: (access_token, expiry_epoch).  Application tokens
# are mailbox-independent so one entry suffices.  Reset to None in tests.
_token_cache: tuple[str, float] | None = None


class GraphConfigError(RuntimeError):
    """GRAPH_* settings missing or the token endpoint refused us."""


@dataclass(frozen=True)
class DraftResult:
    draft_id: str | None
    web_link: str | None
    error: str | None


async def _get_graph_token() -> str:
    """Fetch (or reuse) a client_credentials access token."""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time() + 60:
        return _token_cache[0]

    if not (
        settings.graph_tenant_id
        and settings.graph_client_id
        and settings.graph_client_secret
    ):
        raise GraphConfigError(
            "GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET not configured"
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_LOGIN_BASE}/{settings.graph_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    if resp.status_code != 200:
        raise GraphConfigError(
            f"Graph token endpoint {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    token = str(data["access_token"])
    _token_cache = (token, time.time() + int(data.get("expires_in", 3600)))
    return token


async def create_outlook_draft(
    *,
    mailbox: str,
    subject: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    body_html: str,
    attachments: list[Attachment],
) -> DraftResult:
    """Create a draft (with inline fileAttachments) in ``mailbox``."""
    if not mailbox:
        return DraftResult(None, None, "GRAPH_DRAFT_MAILBOX not configured")

    try:
        token = await _get_graph_token()
    except (GraphConfigError, httpx.HTTPError) as exc:
        return DraftResult(None, None, f"Graph auth failed: {exc}")

    def _addrs(vals: list[str]) -> list[dict]:
        return [{"emailAddress": {"address": a}} for a in vals]

    payload: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": _addrs(to),
    }
    if cc:
        payload["ccRecipients"] = _addrs(cc)
    if bcc:
        payload["bccRecipients"] = _addrs(bcc)
    if attachments:
        payload["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att.filename,
                "contentType": att.content_type,
                "contentBytes": base64.b64encode(att.content).decode("ascii"),
            }
            for att in attachments
        ]

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/users/{mailbox}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return DraftResult(None, None, f"Graph network error: {exc!r}")

    if resp.status_code != 201:
        return DraftResult(None, None, f"Graph {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    logger.info("comms: outlook draft created in %s: %s", mailbox, str(data.get("id", ""))[:32])
    return DraftResult(data.get("id"), data.get("webLink"), None)


# ---------------------------------------------------------------------------
# Resend API transport  (ported from the original customer_email.py, d3a052e)
# ---------------------------------------------------------------------------
#
# The customer_doc "sent" path.  A real Resend call happens ONLY after the
# two-key kill switch + FROM allowlist all pass (see run_customer_email_policy)
# AND the API key is set — an empty key blocks, exactly like the original.


async def _post_to_resend(
    *,
    api_key: str,
    api_url: str,
    from_addr: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachments: list[Attachment],
) -> tuple[str | None, str | None]:
    """Make the actual Resend network call.  Returns ``(message_id, error)``."""
    payload: dict = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": body_html,
    }
    if body_text:
        payload["text"] = body_text
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if attachments:
        payload["attachments"] = [
            {
                "filename": att.filename,
                "content": base64.b64encode(att.content).decode("ascii"),
            }
            for att in attachments
        ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{api_url}/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            return None, f"Resend network error: {exc!r}"

    if 200 <= resp.status_code < 300:
        return resp.json().get("id"), None
    return None, f"Resend {resp.status_code}: {resp.text[:500]}"


# ---------------------------------------------------------------------------
# Email templates  (ported from saebooks/services/email.py + templates/emails/)
# ---------------------------------------------------------------------------

# comms.py lives in saebooks_web/; templates/emails/ is at the repo root.
_EMAIL_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"

# Template-name whitelist: bare name (as the engine passes in meta.template) →
# the .html file this module owns.  Membership doubles as the anti-arbitrary-
# render gate: only these names reach the loader (400 otherwise).
_EMAIL_TEMPLATES: dict[str, str] = {
    "magic_link_email": "magic_link_email.html",
}

_email_env: jinja2.Environment | None = None


def get_email_env() -> jinja2.Environment:
    """Return the process-wide email Jinja2 environment (lazy singleton).

    autoescape=True — these are HTML emails, so ctx values are HTML-escaped
    (matching the engine's ``saebooks/services/email.py``).
    """
    global _email_env
    if _email_env is None:
        _email_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_EMAIL_TEMPLATE_DIR)),
            autoescape=True,
        )
    return _email_env


def render_email_template(template: str, context: dict) -> str:
    """Render a whitelisted email template with ``context``.

    ``template`` is the bare name the engine sends (e.g. ``"magic_link_email"``,
    no extension).  Raises ``KeyError`` for a name outside the whitelist — the
    route maps that to HTTP 400.
    """
    filename = _EMAIL_TEMPLATES[template]  # KeyError → 400 at the route
    tmpl = get_email_env().get_template(filename)
    # The email env is a bare jinja2.Environment (not Jinja2Templates), so it
    # doesn't get the current_brand() global the rest of the app registers —
    # supply the active Brand directly so email templates can read brand.name
    # (deployment-wide SAEBOOKS_BRAND, same as every other rendered page).
    render_context = {"brand": current_brand(), **context}
    return tmpl.render(**render_context)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

# Per-tenant FROM allowlist — ported verbatim from the original
# customer_email.py (git d3a052e).  Phase-0 hardcode; anything not in the
# allowlist for its tenant is BLOCKED.  Only consulted for kind=customer_doc.
_TENANT_FROM_ALLOWLIST: dict[str, set[str]] = {
    "f6c01a9d-0d41-426c-aa61-e9e60e8a7995": {  # Sauer Pty Ltd ATF Saueesti Trust
        "admin@saee.com.au",
        "accounts@saee.com.au",
    },
}


async def run_customer_email_policy(
    *,
    tenant_id: str | None,
    from_addr: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachments: list[Attachment],
    tenant_outbound_enabled: bool,
) -> tuple[str, str | None, str | None]:
    """The customer_doc policy — ported from the original ``send_customer_email``.

    Returns ``(outcome, provider_id, detail)``, outcome ∈ ``sent`` | ``drafted``
    | ``blocked`` | ``failed``.  Never raises: a transport failure is reported
    as ``failed`` (200), mirroring the original which returned mode='failed'
    rather than raising.  The engine facade records that outcome verbatim.

    Decision order (faithful to the original):

    1. DRAFT MODE overrides everything — park as a Graph draft; the two-key
       kill switch is not even consulted.  Draft failure → ``failed``.
    2. Two-key kill switch + FROM allowlist, AND'd — any failing gate blocks:
       * ``SAEBOOKS_EMAIL_SEND_ENABLED`` env key false;
       * per-tenant ``tenant_outbound_enabled`` fact (from meta) false;
       * ``from_addr`` not in the tenant's FROM allowlist.
    3. Empty ``RESEND_API_KEY`` → blocked.
    4. Past all gates → the actual Resend send (``sent`` / ``failed``).
    """
    # ── DRAFT MODE — park in the operator's Outlook drafts for review ──
    if settings.customer_email_draft_mode:
        draft = await create_outlook_draft(
            mailbox=settings.graph_draft_mailbox,
            subject=subject,
            to=to,
            cc=cc,
            bcc=bcc,
            body_html=body_html,
            attachments=attachments,
        )
        if draft.draft_id:
            reason = f"draft mode: saved to Outlook drafts in {settings.graph_draft_mailbox}"
            logger.info("comms customer_doc DRAFTED: %s", reason)
            return "drafted", draft.draft_id, reason
        logger.error("comms customer_doc DRAFT FAILED: %s", draft.error)
        return "failed", None, f"draft mode: {draft.error}"

    # ── Two-key kill switch + FROM allowlist (AND'd) ──
    allowlist = _TENANT_FROM_ALLOWLIST.get(str(tenant_id), set())
    block_reasons: list[str] = []
    if not settings.customer_email_send_enabled:
        block_reasons.append("env SAEBOOKS_EMAIL_SEND_ENABLED is not true")
    if not tenant_outbound_enabled:
        block_reasons.append(
            f"tenants.outbound_email_enabled is false for tenant {tenant_id}"
        )
    if from_addr not in allowlist:
        block_reasons.append(
            f"from_addr {from_addr!r} not in tenant allowlist {sorted(allowlist) or '[]'}"
        )
    if block_reasons:
        reason = "; ".join(block_reasons)
        logger.warning("comms customer_doc BLOCKED: %s", reason)
        return "blocked", None, reason

    # ── Transport must be configured — empty key blocks (as in the original) ──
    if not settings.resend_api_key:
        return "blocked", None, "RESEND_API_KEY is empty"

    # ── Past all gates — make the actual Resend send ──
    provider_id, resend_error = await _post_to_resend(
        api_key=settings.resend_api_key,
        api_url=settings.resend_api_url,
        from_addr=from_addr,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        attachments=attachments,
    )
    if provider_id:
        return "sent", provider_id, "sent via Resend"
    logger.error("comms customer_doc SEND FAILED: %s", resend_error)
    return "failed", None, resend_error


async def run_legacy_mailer_policy(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachments: list[Attachment],
) -> tuple[str, str | None, str]:
    """The non-customer policy (magic_link / raw / billing_receipt).

    LEGACY MAILER semantics — NO customer kill switch, NEVER drafted: send via
    SMTP whenever ``SMTP_HOST`` is configured, ``blocked`` otherwise.  Login
    links and receipts keep delivering even while customer email is in draft
    mode.  Raises ``TransportError`` on an SMTP delivery failure (the old
    mailer raised ``EmailError``; the engine ``email`` facade re-raises it →
    HTTP 502).
    """
    if not settings.smtp_host:
        reason = "SMTP_HOST is empty"
        logger.warning("comms legacy BLOCKED: %s", reason)
        return "blocked", None, reason

    try:
        result = await send_smtp(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            html=body_html,
            text=body_text,
            attachments=attachments,
            sender=sender,
        )
    except EmailError as exc:
        logger.error("comms legacy SEND FAILED: %s", exc)
        raise TransportError(str(exc)) from exc

    detail = f"sent via SMTP host={settings.smtp_host} ({result.mode})"
    return "sent", result.message_id, detail


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

router = APIRouter()

_VALID_KINDS: frozenset[str] = frozenset(
    {"customer_doc", "magic_link", "billing_receipt", "raw"}
)


def _token_ok(request: Request) -> bool:
    """True when COMMS_TOKEN is unset (dev) or the header matches it.

    Constant-time comparison via ``hmac.compare_digest`` to avoid leaking the
    token through timing.
    """
    expected = settings.comms_token
    if not expected:
        return True  # dev mode — endpoint open, rely on network isolation
    provided = request.headers.get("x-comms-token", "")
    return hmac.compare_digest(provided, expected)


def _coerce_recipients(value: object) -> list[str] | None:
    """Normalise ``to`` (str | list[str]) to a non-empty list, or None."""
    if isinstance(value, str):
        recips = [value] if value.strip() else []
    elif isinstance(value, list):
        recips = [str(v) for v in value if isinstance(v, str) and v.strip()]
    else:
        return None
    return recips or None


@router.post("/internal/comms/send", include_in_schema=False)
async def send_comms(request: Request) -> Response:
    """Send (or draft, or block) one outbound email (server-to-server).

    See the module docstring for the exact contract the engine depends on.
    """
    # Auth first — never do any work for an unauthenticated caller.
    if not _token_ok(request):
        return JSONResponse(
            {"detail": "invalid or missing comms token"}, status_code=401
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"detail": "request body must be a JSON object"}, status_code=400
        )

    kind = payload.get("kind")
    if kind not in _VALID_KINDS:
        return JSONResponse(
            {"detail": f"invalid kind: {kind!r}"}, status_code=400
        )

    to = _coerce_recipients(payload.get("to"))
    if to is None:
        return JSONResponse(
            {"detail": "at least one recipient (to) is required"}, status_code=400
        )

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    subject = str(payload.get("subject") or "").strip()
    body_text = payload.get("body_text")
    if body_text is not None:
        body_text = str(body_text)

    # ── Body assembly — kind only affects this step ──
    if kind == "magic_link":
        template = meta.get("template")
        if not isinstance(template, str) or template not in _EMAIL_TEMPLATES:
            return JSONResponse(
                {"detail": f"unknown email template: {template!r}"},
                status_code=400,
            )
        context = meta.get("context")
        if not isinstance(context, dict):
            context = {}
        body_html = render_email_template(template, context)
    else:
        body_html = str(payload.get("body_html") or "")

    if not subject:
        return JSONResponse({"detail": "subject required"}, status_code=400)
    if not body_html.strip():
        return JSONResponse({"detail": "body_html required"}, status_code=400)

    # ── Attachments — decode base64 ──
    attachments: list[Attachment] = []
    for raw in payload.get("attachments") or []:
        if not isinstance(raw, dict) or "content_b64" not in raw:
            return JSONResponse(
                {"detail": "each attachment needs filename + content_b64"},
                status_code=400,
            )
        try:
            content = base64.b64decode(raw["content_b64"], validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse(
                {"detail": f"invalid base64 in attachment {raw.get('filename')!r}"},
                status_code=400,
            )
        attachments.append(
            Attachment(
                filename=str(raw.get("filename") or "attachment"),
                content=content,
                content_type=str(raw.get("content_type") or "application/octet-stream"),
            )
        )

    cc = [str(v) for v in (meta.get("cc") or []) if isinstance(v, str)]
    bcc = [str(v) for v in (meta.get("bcc") or []) if isinstance(v, str)]

    # ── Dispatch on kind — the customer kill switch is customer_doc ONLY ──
    try:
        if kind == "customer_doc":
            # sender: meta.from_addr (engine's field), meta.from fallback alias.
            from_addr = str(
                meta.get("from_addr") or meta.get("from") or settings.smtp_from
            )
            outcome, provider_id, detail = await run_customer_email_policy(
                tenant_id=meta.get("tenant_id"),
                from_addr=from_addr,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                attachments=attachments,
                tenant_outbound_enabled=bool(meta.get("tenant_outbound_enabled")),
            )
        else:
            # sender: meta.sender (engine's field), meta.from fallback alias.
            sender = str(
                meta.get("sender") or meta.get("from") or settings.smtp_from
            )
            outcome, provider_id, detail = await run_legacy_mailer_policy(
                sender=sender,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                attachments=attachments,
            )
    except TransportError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    return JSONResponse(
        {"outcome": outcome, "provider_id": provider_id, "detail": detail}
    )
