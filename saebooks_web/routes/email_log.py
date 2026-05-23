"""Email send-log views — read-only UI onto /api/v1/email-log.

Routes
------
GET /email-log              — paginated list with filter chips
GET /email-log/{log_id}     — single attempt detail (body + envelope + status)
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

logger = logging.getLogger(__name__)
router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> bool:
    return bool(request.session.get("user_id") or request.session.get("user_email"))


_STATUS_BADGES = {
    "sent":    ("badge-paid",    "Sent"),
    "queued":  ("badge-pending", "Queued"),
    "blocked": ("badge-draft",   "Blocked"),
    "failed":  ("badge-overdue", "Failed"),
}


@router.get("/email-log", response_class=HTMLResponse, response_model=None)
async def email_log_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    doc_type: str | None = None,
    status: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, str | int] = {"limit": limit, "offset": offset}
    if doc_type:
        params["doc_type"] = doc_type
    if status:
        params["status"] = status

    async with api_client(request) as client:
        resp = await client.get("/api/v1/email-log", params=params)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Upstream error")

    data = resp.json()
    items = data.get("items", [])
    total = data.get("total", 0)
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    return _TEMPLATES.TemplateResponse(
        request,
        "email_log/list.html",
        {
            "items":         items,
            "total":         total,
            "limit":         limit,
            "offset":        offset,
            "prev_offset":   prev_offset,
            "next_offset":   next_offset,
            "filter_doc_type": doc_type or "",
            "filter_status":   status or "",
            "status_badges": _STATUS_BADGES,
        },
    )


@router.get("/email-log/export.csv", response_model=None)
async def email_log_export_csv(
    request: Request,
    doc_type: str | None = None,
    status: str | None = None,
) -> Response | RedirectResponse:
    """Proxy to /api/v1/email-log/export.csv — streams CSV to the browser."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    params: dict[str, str] = {}
    if doc_type:
        params["doc_type"] = doc_type
    if status:
        params["status"] = status
    async with api_client(request) as client:
        resp = await client.get("/api/v1/email-log/export.csv", params=params)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Upstream error")
    return Response(
        content=resp.content,
        media_type="text/csv",
        headers={
            "Content-Disposition": resp.headers.get("content-disposition", 'attachment; filename="email_send_log.csv"'),
            "Cache-Control":       "no-store",
        },
    )


@router.get("/email-log/{log_id}/attachment/{idx}", response_model=None)
async def email_log_attachment(
    request: Request, log_id: str, idx: int
) -> Response | RedirectResponse:
    """Proxy to /api/v1/email-log/{id}/attachment/{idx} — streams PDF."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/email-log/{log_id}/attachment/{idx}")
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Upstream error")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers={
            "Content-Disposition": resp.headers.get("content-disposition", f'inline; filename="attachment-{idx}"'),
            "Cache-Control":       "private, max-age=0, must-revalidate",
        },
    )


@router.get("/email-log/{log_id}", response_class=HTMLResponse, response_model=None)
async def email_log_detail(
    request: Request, log_id: str
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/email-log/{log_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        raise HTTPException(404, "Log entry not found")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Upstream error")

    entry = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "email_log/detail.html",
        {
            "entry":         entry,
            "status_badges": _STATUS_BADGES,
        },
    )
