"""/recurring — aggregator hub for recurring transaction templates.

QBO surfaces "Recurring transactions" as a cross-cutting Accounting verb
([[saebooks-qbo-nav-reference]]). SAE Books currently has:
  - recurring_invoices (full CRUD + generate)
  - journal_templates (template-of-journal-entry, manually recurred)

This page aggregates both onto one landing so users have one place to find
"things that repeat". Recurring bills + recurring expenses are flagged as
"coming soon" — backend not yet built.

Auth guard mirrors dashboard.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


async def _fetch_items(client, path: str, params: dict | None = None) -> list[dict]:
    try:
        resp = await client.get(path, params=params or {})
        if resp.is_success:
            return resp.json().get("items", [])
    except Exception:
        pass
    return []


@router.get("/recurring", response_class=HTMLResponse, response_model=None)
async def recurring_hub(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        recurring_invoices, journal_templates, contacts_raw = await asyncio.gather(
            _fetch_items(client, "/api/v1/recurring_invoices",
                         {"page": 1, "page_size": 100}),
            _fetch_items(client, "/api/v1/journal_templates",
                         {"page": 1, "page_size": 100}),
            _fetch_items(client, "/api/v1/contacts",
                         {"page": 1, "page_size": 500}),
        )

    cmap = {c.get("id"): c.get("name") or "" for c in contacts_raw if c.get("id")}

    # Counters
    active_invoices = [r for r in recurring_invoices
                       if (r.get("status") or "").upper() == "ACTIVE"]
    paused_invoices = [r for r in recurring_invoices
                       if (r.get("status") or "").upper() == "PAUSED"]

    ctx = {
        "request": request,
        "recurring_invoices": recurring_invoices,
        "active_invoices_count": len(active_invoices),
        "paused_invoices_count": len(paused_invoices),
        "journal_templates": journal_templates,
        "journal_templates_count": len(journal_templates),
        "contacts_by_id": cmap,
    }
    return _TEMPLATES.TemplateResponse(request, "recurring/hub.html", ctx)
