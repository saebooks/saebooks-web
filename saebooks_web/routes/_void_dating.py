"""Dated voids + the lodged-BAS warning, shared by every document void
route (batch 5, P0-20).

The void forms carry an optional ``reversal_date`` (default today) and,
after a warning, ``confirm_lodged_period``. The engine refuses a reversal
that lands in a BAS period marked lodged with 409 ``lodged_period``; the
route then shows ``_partials/void_lodged_confirm.html`` so the user can
pick a later date or confirm.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def void_body(form: Any) -> dict | None:
    """JSON body for the engine void call, or None when the form sent
    neither field (keeps old callers byte-identical)."""
    body: dict = {}
    rd = str(form.get("reversal_date", "") or "").strip()
    if rd:
        body["reversal_date"] = rd
    if str(form.get("confirm_lodged_period", "") or "") in ("1", "true", "on"):
        body["confirm_lodged_period"] = True
    return body or None


def lodged_conflict(resp: Any) -> dict | None:
    """The engine's lodged-period refusal, or None for any other response."""
    if resp.status_code != 409:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict) and body.get("code") == "lodged_period":
        return body
    return None


def render_lodged_confirm(
    request: Request,
    *,
    conflict: dict,
    action_url: str,
    back_url: str,
    version: str,
    what: str,
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "_partials/void_lodged_confirm.html",
        {
            "conflict": conflict,
            "action_url": action_url,
            "back_url": back_url,
            "version": version,
            "what": what,
        },
        status_code=409,
    )
