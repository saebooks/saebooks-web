"""Duplicate supplier invoice warning on the bill form (gap P2-13).

``GET /bills/duplicate-check?contact_id=…&supplier_reference=…`` is called
by the bill form (HTMX) as the supplier invoice number is typed. It asks the
engine (``GET /api/v1/bill-checks/duplicates``) for other bills from the
same supplier with the same number — spacing, dashes and letter case
ignored — and shows a plain warning with links. It never blocks saving:
suppliers do reuse numbers, and the user decides.

Registered before the bills router so ``/bills/duplicate-check`` is not
read as ``/bills/{bill_id}``.
"""
from __future__ import annotations

import uuid
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


def _uuid_or_none(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


@router.get("/bills/duplicate-check", response_class=HTMLResponse, response_model=None)
async def duplicate_check(
    request: Request,
    contact_id: str = "",
    supplier_reference: str = "",
    exclude_bill_id: str = "",
) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    contact = _uuid_or_none(contact_id)
    ref = supplier_reference.strip()[:64]
    if not contact or not ref:
        return HTMLResponse("")
    params = {"contact_id": contact, "supplier_reference": ref}
    exclude = _uuid_or_none(exclude_bill_id)
    if exclude:
        params["exclude_bill_id"] = exclude
    duplicates: list[dict] = []
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/bill-checks/duplicates", params=params)
        if resp.is_success:
            duplicates = resp.json().get("duplicates", [])
    except ModuleUnavailable:
        return HTMLResponse("")
    if not duplicates:
        return HTMLResponse("")
    return _TEMPLATES.TemplateResponse(
        request, "b15/bill_duplicates.html", {"duplicates": duplicates, "reference": ref}
    )
