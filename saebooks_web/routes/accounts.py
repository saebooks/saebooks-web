"""Accounts (Chart of Accounts) list + detail views — Lane D cycle 9.

GET /accounts
    Renders templates/accounts/list.html (full page) or
    templates/accounts/_table.html (HTMX fragment when HX-Request header present).
    Query params: account_type, search, limit (default 200), offset.
    Calls GET /api/v1/accounts with matching params.

GET /accounts/{id}
    Renders templates/accounts/detail.html.
    Calls GET /api/v1/accounts/{id}.

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


@router.get("/accounts", response_class=HTMLResponse, response_model=None)
async def accounts_list(
    request: Request,
    account_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the accounts list page (full or HTMX fragment)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if account_type:
        params["account_type"] = account_type

    error: str | None = None
    accounts: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/accounts", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            accounts = payload.get("items", [])
            total = payload.get("total", len(accounts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset: int | None = offset - limit if offset > 0 else None
    next_offset: int | None = offset + limit if offset + limit < total else None

    ctx = {
        "accounts": accounts,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filter_account_type": account_type or "",
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "error": error,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "accounts/_table.html" if is_htmx else "accounts/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/accounts/{account_id}", response_class=HTMLResponse, response_model=None)
async def account_detail(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single account detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/accounts/{account_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "accounts/detail.html",
                {"account": None, "error": "Account not found"},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "accounts/detail.html",
                {"account": None, "error": f"API error: HTTP {resp.status_code}"},
                status_code=resp.status_code,
            )

    account = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "accounts/detail.html",
        {"account": account, "error": None},
    )
