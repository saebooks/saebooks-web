"""Asset disposal preview fragment (gap P2-4, 2026-11-13 sweep).

``GET /fixed-assets/{id}/disposal-preview?disposal_date=…&proceeds=…``
shows, before anything is recorded, the depreciation still to catch up,
the book value at the disposal date, the GST in the sale price (when the
business is registered) and the gain or loss — from the engine's read-only
``GET /api/v1/fixed_assets/{id}/disposal-preview``. Loaded into the dispose
form as the date and price are typed.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
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


@router.get(
    "/fixed-assets/{asset_id}/disposal-preview", response_class=HTMLResponse, response_model=None
)
async def disposal_preview(
    request: Request, asset_id: uuid.UUID, disposal_date: str = "", proceeds: str = ""
) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    ctx: dict = {"preview": None, "error": None}
    if not disposal_date:
        return _TEMPLATES.TemplateResponse(request, "b15/disposal_preview.html", ctx)
    try:
        price = Decimal((proceeds or "0").replace(",", "").replace("$", "").strip() or "0")
    except InvalidOperation:
        ctx["error"] = "The price is not a number."
        return _TEMPLATES.TemplateResponse(request, "b15/disposal_preview.html", ctx)
    try:
        async with api_client(request) as client:
            resp = await client.get(
                f"/api/v1/fixed_assets/{asset_id}/disposal-preview",
                params={"disposal_date": disposal_date, "sale_price": str(price)},
            )
        if resp.is_success:
            ctx["preview"] = resp.json()
        else:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = None
            ctx["error"] = detail if isinstance(detail, str) else "The preview could not be worked out."
    except ModuleUnavailable:
        ctx["error"] = "The preview is not available just now."
    return _TEMPLATES.TemplateResponse(request, "b15/disposal_preview.html", ctx)
