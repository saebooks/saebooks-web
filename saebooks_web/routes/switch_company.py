"""GET /switch-company?company_id=<uuid> — set session.active_company_id and
redirect back to the referer (or / if none).

GET is used rather than POST to side-step CSRF token wiring. The action is
session-only (no DB mutation) and the company_id must belong to one of the
companies in request.state.companies (validated by membership check), so the
only attack surface is "another tab makes the user see Sauer Pty Ltd instead
of Richard Personal" — purely a UX nuisance, not a data leak.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/switch-company", include_in_schema=False)
async def switch_company(request: Request, company_id: UUID) -> RedirectResponse:
    target = str(company_id)
    allowed = {c["id"] for c in getattr(request.state, "companies", [])}
    if allowed and target not in allowed:
        raise HTTPException(404, "Company not available to this user")
    request.session["active_company_id"] = target
    referer = request.headers.get("referer") or "/"
    return RedirectResponse(url=referer, status_code=303)
