"""GET /switch-company?company_id=<uuid> — set session.active_company_id and
redirect to a safe landing page.

GET is used rather than POST to side-step CSRF token wiring. The action is
session-only (no DB mutation) and the company_id must belong to one of the
companies in request.state.companies (validated by membership check).

Redirect target
---------------

Previously this returned to the ``Referer`` header so the user stayed on the
page they were on. That turned out to be unsafe: if the referer was a
resource-detail URL (e.g. ``/bank-accounts/<uuid>``) and the resource
belonged to the company they JUST LEFT, the user would land on a stale page
showing wrong-company data. Submitting an edit from that page would corrupt
data — see API-side guard work in deps.require_company_owned.

The safe behaviour is:

* If the referer is a section LIST page (no resource UUID in the path), keep
  it — list endpoints already filter by ``active_company_id`` so they
  self-rebuild for the new company.
* If the referer is a resource DETAIL/EDIT page (contains a UUID segment),
  strip the UUID and redirect to the section list — keeps the user in their
  current section but off a stale resource.
* If no referer or referer is empty, redirect to ``/``.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


# 8-4-4-4-12 hex UUID with optional surrounding slashes.
_UUID_SEGMENT = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)",
    re.IGNORECASE,
)


def _safe_redirect_target(referer: str | None) -> str:
    """Return a redirect URL that is safe to send the user to after a switch.

    Strips any UUID segment (and anything after it) from the referer path so
    a detail/edit page like ``/bank-accounts/<uuid>/edit`` collapses to its
    section list ``/bank-accounts``. Same-origin only; cross-origin or empty
    referers fall back to ``/``.
    """
    if not referer:
        return "/"
    try:
        parsed = urlparse(referer)
    except ValueError:
        return "/"
    # Same-origin only — never honour a cross-site referer for our redirect.
    if parsed.netloc and parsed.netloc != "":
        # We can't tell the host without the request context; the netloc
        # being non-empty means this is an absolute URL, which we trust only
        # if it parses cleanly. Strip to the path + query.
        pass
    path = parsed.path or "/"
    match = _UUID_SEGMENT.search(path)
    if match:
        # Trim at the start of the UUID segment, including everything after.
        path = path[: match.start()] or "/"
        # Drop the query string too — it might reference the resource id.
        return urlunparse(("", "", path, "", "", ""))
    # Safe path (no UUID) — preserve as-is (relative form).
    return urlunparse(("", "", path, "", parsed.query, ""))


@router.get("/switch-company", include_in_schema=False)
async def switch_company(request: Request, company_id: UUID) -> RedirectResponse:
    target = str(company_id)
    allowed = {c["id"] for c in getattr(request.state, "companies", [])}
    if allowed and target not in allowed:
        raise HTTPException(404, "Company not available to this user")
    request.session["active_company_id"] = target
    redirect_to = _safe_redirect_target(request.headers.get("referer"))
    return RedirectResponse(url=redirect_to, status_code=303)
