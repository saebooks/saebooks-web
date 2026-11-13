"""Default email wording for customer documents (b06 — P0-43, P1-38).

Before b06 the quote and invoice composers carried one business's wording
in code: a director's name and mobile number, admin@saee.com.au and an
"SAE-2026-" number prefix. Now the subject, body, signature, sender name,
reply-to and the From choices all come from the company's email settings
(``GET /api/v1/setup/document-settings``) and the sending status comes from
the comms policy itself, so the screen says plainly whether an email will
actually leave.

Templates are plain text with ``{placeholders}``; :func:`render` fills them
and :func:`to_html` turns blank-line separated paragraphs into HTML.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta
from typing import Any

import httpx

from saebooks_web.comms import customer_email_status

logger = logging.getLogger("saebooks_web.email_defaults")

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def render(text: str, values: dict[str, Any]) -> str:
    """Fill ``{name}`` placeholders; missing ones become empty, and a line
    left empty only because its placeholders were blank is dropped."""
    out: list[str] = []
    for line in (text or "").split("\n"):
        had = bool(_PLACEHOLDER.search(line))
        new = _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1)) or ""), line)
        if had and line.strip() and not new.strip():
            continue
        out.append(new)
    return "\n".join(out).strip("\n")


def to_html(text: str) -> str:
    paras = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return "\n".join(
        "<p>" + "<br>".join(html.escape(ln) for ln in p.split("\n")) + "</p>" for p in paras
    )


def money(amount: Any, currency: str | None = None) -> str:
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        value = 0.0
    cur = (currency or "AUD").upper()
    symbol = "$" if cur in ("AUD", "NZD", "USD", "CAD", "SGD", "HKD") else ""
    text = f"{symbol}{value:,.2f}"
    return f"{text} {cur}" if cur not in ("AUD",) else text


def nice_date(raw: Any) -> str:
    if not raw:
        return ""
    try:
        d = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return str(raw)
    return d.strftime("%-d %B %Y")


async def settings_for(client: httpx.AsyncClient) -> dict[str, Any]:
    """The company's email/document settings, or {} — composing an email
    must still work (with neutral defaults) if the settings can't be read."""
    try:
        resp = await client.get("/api/v1/setup/document-settings")
    except Exception:
        logger.warning("could not load document settings", exc_info=True)
        return {}
    if not resp.is_success:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def compose(doc_type: str, s: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Defaults for the composer: subject, HTML body, From options, the
    sender identity and the sending status."""
    company = s.get("company") or {}
    vals = {
        "company_name": company.get("name") or "",
        "company_phone": company.get("phone") or "",
        "company_email": company.get("email") or "",
        **values,
    }
    vals["signature"] = render(s.get("signature") or "", vals)
    tmpl = (s.get("templates") or {}).get(doc_type) or {}
    subject = render(tmpl.get("subject") or "{number}", vals).replace("\n", " ")
    body = render(tmpl.get("body") or "{signature}", vals)
    preferred = (s.get("default_from") or {}).get(doc_type)
    status = customer_email_status(
        tenant_id=s.get("tenant_id"),
        from_addr=preferred,
        tenant_outbound_enabled=bool(s.get("outbound_email_enabled")),
        company_addresses=s.get("from_addresses") or [],
    )
    options = list(status["from_options"])
    if preferred and preferred not in options:
        options.insert(0, preferred)
    return {
        "subject": subject,
        "body_html": to_html(body),
        "from_options": options,
        "default_from": status["default_from"],
        "reply_to": s.get("reply_to") or "",
        "from_name": s.get("sender_name") or "",
        "bcc": (company.get("email") or "") if s.get("bcc_self") else "",
        "status": status,
    }


def quote_values(quote: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    valid_until = quote.get("expiry_date")
    if not valid_until and quote.get("issue_date") and quote.get("validity_days"):
        try:
            valid_until = (
                date.fromisoformat(str(quote["issue_date"])[:10])
                + timedelta(days=int(quote["validity_days"]))
            ).isoformat()
        except (ValueError, TypeError):
            valid_until = None
    cur = quote.get("currency")
    return {
        "customer_name": customer.get("name") or "",
        "number": quote.get("number") or "",
        "title": quote.get("title") or "",
        "total": money(quote.get("total"), cur),
        "subtotal": money(quote.get("subtotal"), cur),
        "amount_due": money(quote.get("total"), cur),
        "valid_until": nice_date(valid_until),
        "due_date": "",
    }


def invoice_values(invoice: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    cur = invoice.get("currency")
    try:
        due = float(invoice.get("total") or 0) - float(invoice.get("amount_paid") or 0)
    except (TypeError, ValueError):
        due = 0.0
    return {
        "customer_name": customer.get("name") or "",
        "number": invoice.get("number") or "",
        "title": "",
        "total": money(invoice.get("total"), cur),
        "subtotal": money(invoice.get("subtotal"), cur),
        "amount_due": money(due, cur),
        "due_date": nice_date(invoice.get("due_date")) or "on receipt",
        "valid_until": "",
    }


__all__ = ["compose", "invoice_values", "money", "quote_values", "render", "settings_for", "to_html"]


def send_result_flash(resp: httpx.Response, result: dict[str, Any], to: list[str]) -> tuple[str, str]:
    """Plain-words outcome of a send for the composer banner (HTML-safe)."""
    mode = result.get("mode", "unknown")
    who = html.escape(", ".join(to)) if to else "the customer"
    if mode == "sent":
        ref = html.escape(str(result.get("message_id") or ""))
        return (
            f"Sent to {who}."
            + (f" <span class='ink-3 text-xs'>Reference {ref}</span>" if ref else ""),
            "pos",
        )
    if mode == "drafted":
        return (
            "Saved as a draft for review — it has not gone to the customer yet. "
            f"<span class='ink-3'>{html.escape(str(result.get('reason') or ''))}</span>",
            "warm",
        )
    if mode == "blocked":
        return (
            "Not sent — email sending is not set up yet (see the box above). "
            "A copy of the email and PDF is kept in your email log; you can download "
            "the PDF and send it yourself."
            f"<details class='mt-1 text-xs ink-3'><summary>Technical detail</summary>"
            f"{html.escape(str(result.get('reason') or ''))}</details>",
            "warm",
        )
    if mode == "failed":
        return (
            "Sending failed. Please try again in a few minutes."
            f"<details class='mt-1 text-xs ink-3'><summary>Technical detail</summary>"
            f"{html.escape(str(result.get('errors') or ''))}</details>",
            "neg",
        )
    detail = ""
    try:
        detail = str(resp.json().get("detail") or "")
    except ValueError:
        detail = (resp.text or "")[:300]
    return f"The email was not sent: {html.escape(detail) or 'HTTP ' + str(resp.status_code)}", "neg"
