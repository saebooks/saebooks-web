"""Internal outbound-email service — the web app owns customer comms.

Two-repo extraction (Gitea saebooks/saebooks #31/#32): the accounting engine
is the *accountant* (it owns facts — journals, balances, tax) and the web app
is the *bookkeeper* (it owns presentation and the human-facing side).  This
module is the app side of the outbound-email split.  It ports, behaviourally
verbatim from the engine, three things:

* the outbound-email POLICY — the SACRED two-key kill switch + Outlook draft
  mode from ``saebooks/services/customer_email.py``;
* the SMTP transport from ``saebooks/services/mailer.py`` (the "sent" path);
* the Microsoft Graph draft transport from
  ``saebooks/services/outlook_drafts.py`` (the "drafted" path);

plus the magic-link email template + assembly from
``saebooks/services/email.py`` + ``templates/emails/magic_link_email.html``.

What DID NOT come across (deliberate — it belongs to the accounting engine,
which owns the database and the tenant model):

* ``email_send_log`` audit rows and the .eml audit outbox writes — the engine
  facade records the outcome this endpoint returns;
* the per-tenant ``tenants.outbound_email_enabled`` DB flag and the per-tenant
  FROM allowlist — tenant policy stays in the engine.  This module is
  stateless and DB-free.

The two-key kill switch therefore reduces, in this DB-less module, to its two
ENV keys — exactly the pair the extraction brief names:

* ``SAEBOOKS_EMAIL_SEND_ENABLED`` — key 1, must be explicitly true to send;
* ``SAEBOOKS_EMAIL_DRAFT_MODE``   — key 2, when true parks everything as a
  Graph draft (overrides key 1: while draft mode is on, nothing is sent).

Default for both is false → default-closed: an un-configured deployment
BLOCKS every message (writes nothing to the wire).

Public surface
--------------
``POST /internal/comms/send`` — the server-to-server endpoint the engine's
new comms facades call.  Contract (do not deviate — the engine depends on it):

* If ``COMMS_TOKEN`` is set, header ``X-Comms-Token`` must match it
  (constant-time), else 401.  Empty token → dev mode, open.
* Body (JSON object)::

      {
        "kind": "customer_doc" | "magic_link" | "billing_receipt" | "raw",
        "to": "a@x" | ["a@x", "b@y"],          # required
        "subject": "...",                        # required (magic_link defaults)
        "body_html": "<p>...</p>",               # required except magic_link
        "body_text": "..." | null,               # optional plaintext alt
        "attachments": [                          # optional
          {"filename": "...", "content_b64": "...", "content_type": "..."}
        ],
        "meta": {                                 # optional
          "from": "admin@saee.com.au",            # sender; falls back to SMTP_FROM
          "cc": ["..."], "bcc": ["..."],          # optional
          "magic_link": "https://...",            # magic_link kind: the URL
          "expires_minutes": 15                    # magic_link kind: link TTL
        }
      }

  ``kind`` only affects body assembly: ``magic_link`` renders the ported
  template from ``meta.magic_link`` + ``meta.expires_minutes``; every other
  kind uses the supplied ``body_html``.  ALL kinds then run the SAME policy.
* 200 → ``{"outcome": "sent"|"drafted"|"blocked", "provider_id": str|None,
  "detail": str}``.  ``sent`` = delivered via SMTP; ``drafted`` = parked as a
  Graph draft; ``blocked`` = kill switch refused it (nothing left the box).
* 400 → malformed request (bad kind / no recipient / empty subject-or-body /
  bad attachment base64).
* 502 → ``{"detail": ...}`` transport failure (SMTP delivery error, or Graph
  draft creation failed while in draft mode).
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
# Magic-link template  (ported from saebooks/services/email.py)
# ---------------------------------------------------------------------------

# comms.py lives in saebooks_web/; templates/emails/ is at the repo root.
_EMAIL_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"

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


def render_magic_link(*, magic_link: str, expires_minutes: int) -> str:
    """Render the magic-link login email HTML from the ported template."""
    tmpl = get_email_env().get_template("magic_link_email.html")
    return tmpl.render(magic_link=magic_link, expires_minutes=expires_minutes)


# ---------------------------------------------------------------------------
# Policy  (ported from saebooks/services/customer_email.py)
# ---------------------------------------------------------------------------


async def run_email_policy(
    *,
    from_addr: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachments: list[Attachment],
) -> tuple[str, str | None, str]:
    """Apply the two-key kill switch and route to the chosen transport.

    Returns ``(outcome, provider_id, detail)`` where outcome is one of
    ``"sent"`` | ``"drafted"`` | ``"blocked"``.  Raises ``TransportError`` on
    an infrastructure failure (mapped to HTTP 502 by the route).

    Decision order — ported faithfully from ``send_customer_email``:

    1. DRAFT MODE overrides everything.  When ``SAEBOOKS_EMAIL_DRAFT_MODE`` is
       on, the message is parked as a Graph draft; the send kill switch is not
       even consulted.  A draft-creation failure is a transport failure (502).
    2. Otherwise the send kill switch: a real send happens ONLY when
       ``SAEBOOKS_EMAIL_SEND_ENABLED`` is true.  Not enabled → blocked.
    3. Even when enabled, an empty ``SMTP_HOST`` is "not configured" → blocked
       (mirrors the engine's "RESEND_API_KEY empty → blocked"; never a false
       "sent" that silently lands in a dev outbox).
    4. Past the gates → the actual SMTP send.
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
            logger.info("comms DRAFTED: %s", reason)
            return "drafted", draft.draft_id, reason
        logger.error("comms DRAFT FAILED: %s", draft.error)
        raise TransportError(f"draft mode: {draft.error}")

    # ── Kill switch — key 1 ──
    if not settings.customer_email_send_enabled:
        reason = "env SAEBOOKS_EMAIL_SEND_ENABLED is not true"
        logger.warning("comms BLOCKED: %s", reason)
        return "blocked", None, reason

    # ── Transport must be configured — else block, never a false "sent" ──
    if not settings.smtp_host:
        reason = "SMTP_HOST is empty"
        logger.warning("comms BLOCKED: %s", reason)
        return "blocked", None, reason

    # ── Past all gates — make the actual SMTP send ──
    try:
        result = await send_smtp(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            html=body_html,
            text=body_text,
            attachments=attachments,
            sender=from_addr,
        )
    except EmailError as exc:
        logger.error("comms SEND FAILED: %s", exc)
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

_DEFAULT_MAGIC_LINK_SUBJECT = "Your SAE Books Login Link"
_DEFAULT_MAGIC_LINK_EXPIRES = 15


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
        magic_link = meta.get("magic_link")
        if not magic_link:
            return JSONResponse(
                {"detail": "magic_link kind requires meta.magic_link"},
                status_code=400,
            )
        expires_minutes = meta.get("expires_minutes", _DEFAULT_MAGIC_LINK_EXPIRES)
        body_html = render_magic_link(
            magic_link=str(magic_link), expires_minutes=expires_minutes
        )
        if not subject:
            subject = _DEFAULT_MAGIC_LINK_SUBJECT
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

    from_addr = str(meta.get("from") or settings.smtp_from)
    cc = [str(v) for v in (meta.get("cc") or []) if isinstance(v, str)]
    bcc = [str(v) for v in (meta.get("bcc") or []) if isinstance(v, str)]

    # ── Run the policy ──
    try:
        outcome, provider_id, detail = await run_email_policy(
            from_addr=from_addr,
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
