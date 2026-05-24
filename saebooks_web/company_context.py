"""CompanyContextMiddleware — populate request.state.companies + active
company info so base.html can render the per-page label correctly and the
header dropdown can list the user's available companies.

Runs INSIDE SessionMiddleware (so request.session is available) but
OUTSIDE the route handlers. Adds one upstream call to
``/api/v1/companies`` per request — acceptable cost for a low-traffic
self-hosted app; can be optimised later with a per-session memoised
copy if needed.

Reads:
  - request.session['api_token'] — bearer for the upstream API call
  - request.session['active_company_id'] — user's current pick

Writes (request.state only — does not mutate session here):
  - request.state.companies — list of {id, name} for the dropdown
  - request.state.active_company_id — uuid string or None
  - request.state.active_company_name — display name or None
"""
from __future__ import annotations

import logging

import httpx
from starlette.middleware.base import BaseHTTPMiddleware

from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.company_context")


class CompanyContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.companies = []
        request.state.active_company_id = None
        request.state.active_company_name = None
        try:
            token = request.session.get("api_token") if "session" in request.scope else None
        except Exception:
            token = None
        if token:
            try:
                headers = {"Authorization": f"Bearer {token}"}
                active_id_in_session = request.session.get("active_company_id")
                if active_id_in_session:
                    headers["X-Company-Id"] = str(active_id_in_session)
                async with httpx.AsyncClient(
                    base_url=settings.api_url, headers=headers, timeout=5.0,
                ) as client:
                    r = await client.get(
                        "/api/v1/companies",
                        params={"page": 1, "page_size": 50},
                    )
                if r.status_code == 200:
                    items = []
                    for it in r.json().get("items", []):
                        if it.get("archived_at"):
                            continue
                        items.append({
                            "id": it["id"],
                            "name": it.get("trading_name") or it.get("name") or it.get("legal_name") or "(unnamed)",
                            "created_at": it.get("created_at") or "",
                        })
                    # Order matches the API's get_active_company_id fallback
                    # (oldest company first by created_at). Without this, the
                    # dropdown defaulted to the alphabetically-first company
                    # ("Richard Sauer") while data fetches resolved to the
                    # oldest one ("SAE Engineering") — header label and
                    # displayed data disagreed.
                    items.sort(key=lambda c: c["created_at"])
                    request.state.companies = items
                    active = None
                    if active_id_in_session:
                        active = next(
                            (c for c in items if c["id"] == str(active_id_in_session)),
                            None,
                        )
                    if active is None and items:
                        active = items[0]
                    if active is not None:
                        request.state.active_company_id = active["id"]
                        request.state.active_company_name = active["name"]
            except Exception as exc:
                logger.debug("CompanyContextMiddleware: skipped (%s)", exc)
        return await call_next(request)
