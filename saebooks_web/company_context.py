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
  - request.state.active_company_jurisdiction — jurisdiction code
    ("AU", "EE", ...) or None if it couldn't be resolved

Jurisdiction resolution note: ``CompanyOut`` does not expose
``Company.jurisdiction`` (verified against the engine's
saebooks/api/v1/schemas.py — the field exists on the model but was
never added to the API schema), so it cannot be read directly off the
``/api/v1/companies`` response fetched above. Instead this proxies off
``/api/v1/tax_codes``, whose list endpoint (as of the engine's Packet 4a
change) defaults its jurisdiction filter to the requesting company's own
``Company.jurisdiction``. A one-row fetch with no explicit jurisdiction
param therefore returns codes stamped with the active company's real
jurisdiction. This is a workaround for a genuine gap in the engine's
public schema — the engine should expose ``jurisdiction`` directly on
CompanyOut so callers don't need to infer it from a side effect of
another endpoint.
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
        request.state.active_company_jurisdiction = None
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
                        # Resolve jurisdiction — see module docstring: CompanyOut
                        # doesn't expose Company.jurisdiction, so proxy off the
                        # default (no explicit ``jurisdiction`` param) response
                        # of /api/v1/tax_codes, which the engine now resolves
                        # per-company. One extra low-cost call, same pattern as
                        # the /api/v1/companies call above.
                        try:
                            juris_headers = dict(headers)
                            juris_headers["X-Company-Id"] = active["id"]
                            async with httpx.AsyncClient(
                                base_url=settings.api_url,
                                headers=juris_headers,
                                timeout=5.0,
                            ) as juris_client:
                                jr = await juris_client.get(
                                    "/api/v1/tax_codes",
                                    params={"page_size": 1},
                                )
                            if jr.status_code == 200:
                                juris_items = jr.json().get("items") or []
                                if juris_items:
                                    request.state.active_company_jurisdiction = (
                                        juris_items[0].get("jurisdiction") or "AU"
                                    )
                                else:
                                    # No tax codes at all for this company yet —
                                    # matches the engine's own AU fallback.
                                    request.state.active_company_jurisdiction = "AU"
                        except Exception as juris_exc:
                            logger.debug(
                                "CompanyContextMiddleware: jurisdiction lookup skipped (%s)",
                                juris_exc,
                            )
            except Exception as exc:
                logger.debug("CompanyContextMiddleware: skipped (%s)", exc)
        return await call_next(request)
