"""Bank accounts list and detail views — Lane D cycle 27.

GET  /bank-accounts       — list page (paginated, HTMX-aware)
GET  /bank-accounts/{id}  — bank account detail

Auth guard: redirect to /login (303) if no session token.

Bank accounts are tier-4 read-only views.  No create/edit form in this cycle.

The API uses page/page_size pagination and the prefix is /api/v1/bank_accounts.
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


@router.get("/bank-accounts", response_class=HTMLResponse, response_model=None)
async def bank_accounts_list(
    request: Request,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the bank accounts list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``bank_accounts/_table.html`` partial only.  Otherwise the full page
    (``bank_accounts/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # The API uses page/page_size.
    page_size = limit
    page = (offset // page_size) + 1 if page_size > 0 else 1

    params: dict[str, object] = {"page": page, "page_size": page_size}
    if archived:
        params["archived"] = True

    error: str | None = None
    accounts: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank_accounts", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            accounts = payload.get("items", [])
            total = payload.get("total", len(accounts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "accounts": accounts,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_archived": archived,
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "bank_accounts/_table.html" if is_htmx else "bank_accounts/list.html"

    return _TEMPLATES.TemplateResponse(request, template, ctx)


@router.get("/bank-accounts/{account_id}", response_class=HTMLResponse, response_model=None)
async def bank_account_detail(
    request: Request,
    account_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single bank account detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_accounts/{account_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_accounts/detail.html",
                {"account": None, "error": "Bank account not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_accounts/detail.html",
                {"account": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    account = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "bank_accounts/detail.html",
        {"account": account, "error": None, "flash": flash},
    )
