"""Email-in address on the inbox page (gap P2-24, 2026-11-13 sweep).

The email-in address — forward a receipt or supplier invoice to it and it
lands in the inbox — had to be created by hand on a page few people found.
``GET /inbox/_email-in`` asks the engine for the company's address
(``POST /api/v1/inbox/email-addresses/ensure``, which mints one on first
use and is idempotent) and shows it at the top of the inbox with a copy
button. When the plan does not include email-in, or the server has no mail
domain set up yet, it says so in one plain sentence instead.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)


@router.get("/inbox/_email-in", response_class=HTMLResponse, response_model=None)
async def email_in_card(request: Request) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    state = "error"
    address = None
    params = {}
    company_id = getattr(request.state, "active_company_id", None)
    if company_id:
        params["company_id"] = company_id
    try:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/inbox/email-addresses/ensure", params=params)
        if resp.status_code in (200, 201):
            address = resp.json().get("address")
            state = "ready" if address else "no_domain"
        elif resp.status_code in (402, 403, 404):
            state = "not_on_plan"
    except ModuleUnavailable:
        state = "error"
    return _TEMPLATES.TemplateResponse(
        request, "b15/inbox_email_in.html", {"state": state, "address": address}
    )
