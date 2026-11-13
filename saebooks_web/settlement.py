"""Reads behind the paying and collecting screens (batch 3).

Thin wrappers over the engine's ``/api/v1/settlement`` reads so every screen
shows names, numbers and balances instead of internal IDs:

* :func:`open_documents` — a contact's open bills or invoices, oldest due
  first, with what is still owing (payment form picker, gap P0-14).
* :func:`document_settlement` — one bill or invoice: contact name, supplier
  invoice number, balance due, payments and credits applied (gap P1-2).
* :func:`payment_settlement` — one payment: contact name and, per
  allocation, the document number and what is left on it (gap P1-2).
* :func:`due_date` — the due date a contact's payment terms give (P0-15).
* :func:`supplier_pdf` — the supplier's own invoice attached to a bill, for
  the side-by-side viewer (gap P1-3).

Every function degrades to an empty result when the engine is older than
these reads or a call fails, so a screen still renders — it just shows less.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _get_json(client: Any, path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        resp = await client.get(path, params=params)
    except Exception as exc:  # transport error — the screen still renders
        logger.warning("settlement read %s failed: %s", path, exc)
        return None
    if not resp.is_success:
        if resp.status_code not in (404,):
            logger.warning("settlement read %s -> HTTP %s", path, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        return None


async def get_json(client: Any, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET that never raises: the parsed body, or ``None`` on any failure.
    For best-effort name look-ups that must not break a page."""
    return await _get_json(client, path, params)


async def open_documents(client: Any, contact_id: str, direction: str) -> list[dict[str, Any]]:
    if not contact_id or not direction:
        return []
    body = await _get_json(
        client,
        "/api/v1/settlement/open-documents",
        {"contact_id": contact_id, "direction": direction},
    )
    if not isinstance(body, dict):
        return []
    return list(body.get("items") or [])


async def document_settlement(client: Any, kind: str, document_id: str) -> dict[str, Any] | None:
    path = {"bill": "bills", "invoice": "invoices"}[kind]
    body = await _get_json(client, f"/api/v1/settlement/{path}/{document_id}")
    return body if isinstance(body, dict) else None


async def payment_settlement(client: Any, payment_id: str) -> dict[str, Any] | None:
    body = await _get_json(client, f"/api/v1/settlement/payments/{payment_id}")
    return body if isinstance(body, dict) else None


async def due_date(client: Any, contact_id: str, issue_date: str) -> dict[str, Any] | None:
    if not contact_id or not issue_date:
        return None
    body = await _get_json(
        client,
        "/api/v1/settlement/due-date",
        {"contact_id": contact_id, "issue_date": issue_date},
    )
    return body if isinstance(body, dict) else None


def _is_viewable(att: dict[str, Any]) -> bool:
    ctype = str(att.get("content_type") or "").lower()
    name = str(att.get("filename") or "").lower()
    return ctype == "application/pdf" or ctype.startswith("image/") or name.endswith(".pdf")


async def supplier_pdf(client: Any, entity_kind: str, entity_id: str) -> dict[str, Any] | None:
    """The first live PDF or image attached to a bill (the supplier's own
    invoice — the inbox attaches it on publish), or ``None``."""
    body = await _get_json(
        client, "/api/v1/attachments", {"entity_kind": entity_kind, "entity_id": entity_id}
    )
    if not isinstance(body, list):
        return None
    live = [a for a in body if isinstance(a, dict) and not a.get("archived_at")]
    for att in live:
        if _is_viewable(att):
            return att
    return None
