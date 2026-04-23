"""Contacts list view — first real HTMX page.

GET /contacts
    Renders templates/contacts/list.html, backed by GET /api/v1/contacts
    on the saebooks-api.  Requires an authenticated session.

HTMX extension points (TODO — future cycles):
- Pagination via hx-get with ?offset= query param, swapping the table body
- Inline search with hx-trigger="keyup changed delay:300ms"
- New contact slide-out form via hx-get /contacts/new into a modal
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

# Resolve templates relative to the repo root (parent of this package dir).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


@router.get("/contacts", response_class=HTMLResponse, response_model=None)
async def contacts_list(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the contacts list page.

    Fetches the first 100 contacts from the API and renders them in a table.
    Redirects to /login if the session has no token.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    contacts: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/contacts", params={"limit": 100, "offset": 0})
        if resp.status_code == 401:
            # Token in session is no longer valid (e.g. server restarted).
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            contacts = payload.get("items", [])
            total = payload.get("total", len(contacts))
        else:
            error = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "contacts/list.html",
        {
            "contacts": contacts,
            "total": total,
            "error": error,
        },
    )
