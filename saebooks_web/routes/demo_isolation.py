"""Live RLS isolation-proof card for the ephemeral demo (Packet A1, §6b).

One read-only route that lets a demo visitor try to fetch a *foreign* demo
tenant's contact by id and watch it fail — the honest proof that isolation
is Postgres row-level security (distinct ``tenant_id`` per visitor, FORCE
RLS, ``app.current_tenant`` SET LOCAL per transaction under the
non-BYPASSRLS ``saebooks_app`` role), not a UI filter.

Design constraints (deliberate, do not relax):
  * Zero new *engine* surface. The probe calls the SAME public
    ``GET /api/v1/contacts/<uuid>`` every other route uses, with the
    visitor's OWN session JWT (``api_client``). No cross-tenant capability
    is added — the whole point is that the call returns 404.
  * The uuid is validated and canonicalised (``uuid.UUID``) BEFORE any
    upstream call, and is the ONLY thing interpolated into the path — the
    input can never choose the endpoint. Malformed input → 400, no engine
    call at all.
  * GET-only (uuid as the ``probe`` query param) so no CSRF token dance is
    needed — the operation is read-only and side-effect-free.
  * Demo-only. A non-demo session (no ``demo_tenant_id`` marker written by
    the provisioner) gets 404 — this surface must never exist on the real
    app.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# A syntactically-valid, obviously-fake UUID pre-filled into the input so a
# visitor can hit the button with one click. It belongs to no real tenant;
# the expected outcome is a 404 regardless.
_EXAMPLE_FOREIGN_UUID = "00000000-0000-4000-8000-000000000000"

# Public AGPL engine repo + the alembic path carrying the tenant_isolation
# RLS policy. Plain link — "the receipts" for the isolation claim.
_POLICY_SOURCE_URL = (
    "https://github.com/saebooks/saebooks/tree/master/"
    "saebooks/alembic/versions/"
)


def _demo_identity(request: Request) -> dict | None:
    """Return the visitor's own demo identity, or None if this is not a
    provisioned ephemeral-demo session.

    ``demo_tenant_id`` is the marker the provisioner writes on every
    ephemeral session (see ``_apply_provision_to_session``). Its absence
    means the request is not a demo request and this whole surface must
    stay invisible (404)."""
    tenant_id = request.session.get("demo_tenant_id")
    if not tenant_id:
        return None
    return {
        "tenant_id": str(tenant_id),
        "company_id": str(request.session.get("demo_company_id") or ""),
    }


@router.get("/demo/isolation", response_class=HTMLResponse, response_model=None)
async def demo_isolation(request: Request, probe: str | None = None) -> HTMLResponse:
    """Render the isolation-proof card.

    Without ``probe``: show the card + pre-filled foreign-id input.
    With ``probe=<uuid>``: validate, then GET the contact with the
    visitor's own JWT and render the live outcome (expected 404).
    """
    identity = _demo_identity(request)
    if identity is None:
        # Not a demo session — this surface does not exist here.
        raise HTTPException(status_code=404)

    ctx: dict = {
        "identity": identity,
        "example_uuid": _EXAMPLE_FOREIGN_UUID,
        "policy_source_url": _POLICY_SOURCE_URL,
        "probe_value": probe or _EXAMPLE_FOREIGN_UUID,
        "result": None,
    }

    if probe is not None:
        raw = probe.strip()
        try:
            # Canonicalise: only a well-formed UUID ever reaches the engine,
            # and only its canonical string form is interpolated into the path.
            canonical = str(uuid.UUID(raw))
        except (ValueError, AttributeError):
            # Malformed — reject BEFORE any upstream call.
            ctx["result"] = {"outcome": "invalid"}
            return _TEMPLATES.TemplateResponse(
                request, "demo_isolation.html", ctx, status_code=400
            )

        ctx["probe_value"] = canonical
        status_code: int | None = None
        try:
            async with api_client(request) as client:
                resp = await client.get(f"/api/v1/contacts/{canonical}")
                status_code = resp.status_code
        except Exception:
            status_code = None

        if status_code == 404:
            outcome = "blocked"       # the expected, desired result
        elif status_code == 200:
            outcome = "visible"       # would mean a leak — surfaced honestly
        else:
            outcome = "other"
        ctx["result"] = {"outcome": outcome, "status": status_code}

    return _TEMPLATES.TemplateResponse(request, "demo_isolation.html", ctx)
