"""Due date from a contact's payment terms (batch 3, gap P0-15).

GET /terms/due-date?contact_id=&issue_date=  ->  JSON
    {"due_date": "2026-06-30" | null, "terms_text": "Due 30 days after the end of the month" | null}

Used by the new bill and new invoice forms to fill the due date when the
supplier or customer (or the date) changes, until the user types a due date
themselves. The engine computes it (``/api/v1/settlement/due-date``), so the
rule lives in one place; a blank due date on save is filled by the engine the
same way.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from saebooks_web.api_client import api_client
from saebooks_web.settlement import due_date

router = APIRouter()


@router.get("/terms/due-date", response_model=None)
async def terms_due_date(
    request: Request, contact_id: str = "", issue_date: str = ""
) -> JSONResponse:
    if not request.session.get("api_token"):
        return JSONResponse({"due_date": None, "terms_text": None}, status_code=401)
    async with api_client(request) as client:
        body = await due_date(client, contact_id, issue_date)
    body = body or {}
    return JSONResponse({
        "due_date": body.get("due_date"),
        "terms_text": body.get("terms_text"),
    })
