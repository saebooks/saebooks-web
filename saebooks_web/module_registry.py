"""Module-registry client + caching — M2 app-lane step 9.

Two engine endpoints feed the registry-driven nav:

* ``GET /api/v1/modules`` — unauthenticated, static, identical for every
  user of a deployment. Cached at PROCESS level with a TTL rather than in
  ``request.session``: sessions here are signed COOKIES (starlette
  SessionMiddleware), and stuffing a multi-KB catalogue into every request's
  cookie risks the 4KB browser limit (dropped cookie = logout loop). A
  process cache gives the same "poll once, not per request" behaviour with
  zero cookie growth.
* ``GET /api/v1/modules/usage`` — bearer-gated, per-tenant. Fetched at the
  two trigger points only (login and company switch — never lazily per
  request, so a launch-promo user's effective edition can't go stale
  mid-session) and stored in ``request.session["module_usage"]`` where
  ``features.current_edition(request)`` reads it.

``ModuleRegistryMiddleware`` merges the two into
``request.state.module_registry`` for ``base.html``'s nav partial — same
shape of solution as ``company_context.py``'s ``request.state.companies``,
kept a separate class so it stays independently failable per module.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from saebooks_web.api_client import api_client

logger = logging.getLogger("saebooks_web.module_registry")

#: Registry modules that already have a hand-authored static link in
#: base.html's nav sections (or are core/infra concepts with no standalone
#: page). Filtered out of the dynamic sub-lists so a module never renders
#: twice. Verified against the live /api/v1/modules catalogue (31 modules,
#: 2026-07-16) against base.html's static hrefs. Open item flagged to engine
#: lane: the registry lists ALL modules including statically-wired ones —
#: should it scope to "not yet wired", or grow an `href` field? Until then
#: this set is hand-maintained (step 9's de-dupe requirement).
_ALREADY_STATIC_MODULE_IDS: frozenset[str] = frozenset({
    "document_inbox",       # /inbox — top-level static link (gitea #33)
    "inbox_email",          # part of the inbox surface
    "cashbook",             # bookkeeping_mode drives a whole separate sidebar
    "multi_company",        # /companies
    "ato_sbr",              # /admin/ato-sbr
    "sql_tool",             # /admin/sql-tool
    "qbo_import",           # /admin/imports
    "allocation_rules",     # /allocations
    "inventory",            # /inventory/overview
    "projects_budgets",     # /projects + /budgets
    "asset_v2",             # /fixed-assets
    "extended_audit_modes", # /admin/audit
    "audit_snapshots",      # /admin/audit
    "eid_auth",             # login-page affordance, not a nav destination
    "capture",              # delegated infra module — surfaces via /inbox
    "preaccounting",        # delegated infra module — no standalone page
    "platform",             # delegated infra module — no standalone page
    "comms",                # infra (outbound email) — no standalone page
})

#: Web-side href (and optional icon) for registry modules that DO have a
#: dedicated page but no static nav link. The engine catalogue carries no
#: href field (open item: it should), so an ENTITLED module renders a
#: dynamic nav row only when mapped here — unmapped entitled modules are
#: skipped rather than rendered as dead links. `state="planned"` rows need
#: no href (muted, unlinked) and render regardless.
_MODULE_HREFS: dict[str, dict[str, str]] = {
    # e.g. "bank_feeds": {"href": "/bank-feeds", "icon": "landmark"},
}

_CATALOGUE_TTL_SECONDS = 300.0
_catalogue_cache: dict | None = None
_catalogue_cached_at: float = 0.0


def _catalogue_fresh() -> bool:
    return (
        _catalogue_cache is not None
        and (time.monotonic() - _catalogue_cached_at) < _CATALOGUE_TTL_SECONDS
    )


async def fetch_module_catalogue(request: Request) -> dict:
    """GET /api/v1/modules — static catalogue, process-TTL-cached.

    A fetch failure caches an empty catalogue for the TTL window (negative
    cache) so an engine outage doesn't add a blocking upstream call to
    every page render — the nav simply renders its static links only.
    """
    global _catalogue_cache, _catalogue_cached_at
    if _catalogue_fresh():
        return _catalogue_cache  # type: ignore[return-value]
    catalogue: dict = {"modules": []}
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/modules")
        if resp.is_success:
            catalogue = resp.json()
        else:
            logger.warning(
                "module catalogue fetch returned HTTP %s", resp.status_code
            )
    except Exception as exc:  # ModuleUnavailable or anything else — degrade
        logger.warning("module catalogue unavailable (%s)", exc)
    _catalogue_cache = catalogue
    _catalogue_cached_at = time.monotonic()
    return catalogue


def invalidate_catalogue_cache() -> None:
    """Test hook / manual invalidation."""
    global _catalogue_cache, _catalogue_cached_at
    _catalogue_cache = None
    _catalogue_cached_at = 0.0


async def fetch_module_usage(request: Request) -> dict:
    """GET /api/v1/modules/usage — bearer-gated, per-tenant.

    Called at login (auth.py) and company switch (switch_company.py) ONLY.
    Stores the payload in ``request.session["module_usage"]`` so
    ``features.current_edition(request)`` and the nav middleware can read
    it without another HTTP call. Failure leaves any previous snapshot in
    place (stale beats absent for nav purposes) and never raises.
    """
    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/modules/usage")
        if resp.is_success:
            usage = resp.json()
            request.session["module_usage"] = usage
            return usage
        logger.warning("module usage fetch returned HTTP %s", resp.status_code)
    except Exception as exc:
        logger.warning("module usage unavailable (%s)", exc)
    return request.session.get("module_usage") or {
        "edition": None,
        "effective_edition": None,
        "modules": {},
    }


def merged_module_registry(catalogue: dict, usage: dict) -> dict:
    """Merge the static catalogue (id/label/kind/group/state) with the
    per-user usage payload (entitled/health) into one dict keyed by module
    id, for the sidebar_module_nav.html partial to iterate. Modules that
    already have a static nav link are filtered out (de-dupe, see
    ``_ALREADY_STATIC_MODULE_IDS``).
    """
    usage_modules = usage.get("modules", {}) if isinstance(usage, dict) else {}
    merged: dict[str, dict] = {}
    for mod in catalogue.get("modules", []):
        mid = mod.get("id")
        if not mid or mid in _ALREADY_STATIC_MODULE_IDS:
            continue
        u = usage_modules.get(mid) or {}
        merged[mid] = {
            **mod,
            **_MODULE_HREFS.get(mid, {}),
            "entitled": bool(u.get("entitled", False)),
            "health": u.get("health"),
        }
    return merged


class ModuleRegistryMiddleware(BaseHTTPMiddleware):
    """Set ``request.state.module_registry`` on authenticated requests.

    Reads the process-cached catalogue (one upstream call per TTL window,
    not per request) + the session-cached usage snapshot (fetched at the
    login / company-switch trigger points). No per-request HTTP call once
    the catalogue cache is warm.
    """

    async def dispatch(self, request, call_next):
        request.state.module_registry = {}
        try:
            token = (
                request.session.get("api_token")
                if "session" in request.scope
                else None
            )
        except Exception:
            token = None
        if token:
            try:
                catalogue = await fetch_module_catalogue(request)
                usage = request.session.get("module_usage") or {}
                request.state.module_registry = merged_module_registry(
                    catalogue, usage
                )
            except Exception as exc:
                # Never let nav decoration break a page render.
                logger.warning("module registry skipped (%s)", exc)
        return await call_next(request)
