"""Needs-attention card — the first thing on the dashboard (gap P1-32).

``GET /dashboard/attention`` renders a compact card from the engine's
``GET /api/v1/attention`` (counts and amounts worked out in the engine:
overdue invoices, bills due this week and overdue, bank lines to
reconcile, documents waiting in the inbox, drafts). The dashboard loads it
with HTMX, so the card arrives on its own and a slow or failed engine call
never holds up — or breaks — the rest of the dashboard.

Each line links to the list that shows exactly those documents. Lines with
nothing waiting are left out; when nothing at all is waiting the card says
so in one line. The partial also stores a count-only snapshot in the
browser (``localStorage``) so the offline page can show what was waiting
the last time the phone had signal (gap P2-25).
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

logger = logging.getLogger("saebooks_web.attention")

router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)


def attention_lines(payload: dict) -> list[dict]:
    """Engine items -> card lines (label, link, count, amount), in the
    order a bookkeeper works: money owed to you, money you owe, the bank,
    the inbox, then unfinished drafts. Zero-count items are dropped."""
    meta = {
        "overdue_invoices": (_("Overdue invoices"), "/invoices?status=overdue", "neg"),
        "overdue_bills": (_("Overdue bills"), "/bills?status=overdue", "neg"),
        "bills_due_this_week": (_("Bills due this week"), "/bills?status=due-soon", "warm"),
        "bank_lines_to_reconcile": (_("Bank lines to reconcile"), "/reconciliation", "warm"),
        "inbox_waiting": (_("Receipts and bills in the inbox"), "/inbox", "sae"),
        "draft_invoices": (_("Draft invoices not sent"), "/invoices?status=draft", "ink"),
        "draft_bills": (_("Draft bills not entered"), "/bills?status=draft", "ink"),
    }
    lines: list[dict] = []
    for item in payload.get("items", []):
        key = item.get("key")
        if key not in meta or not item.get("count"):
            continue
        label, href, tone = meta[key]
        lines.append(
            {
                "key": key,
                "label": label,
                "href": href,
                "tone": tone,
                "count": int(item.get("count") or 0),
                "amount": item.get("amount"),
                "oldest": item.get("oldest"),
            }
        )
    order = list(meta)
    lines.sort(key=lambda line: order.index(line["key"]))
    return lines


@router.get("/dashboard/attention", response_class=HTMLResponse, response_model=None)
async def attention_card(request: Request) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    payload: dict = {}
    failed = False
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/attention")
        if resp.is_success:
            payload = resp.json()
        else:
            failed = True
    except ModuleUnavailable:
        failed = True
    except Exception:
        logger.warning("needs-attention card failed", exc_info=True)
        failed = True
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/attention_card.html",
        {
            "lines": attention_lines(payload),
            "as_at": payload.get("as_at"),
            "failed": failed,
        },
    )
