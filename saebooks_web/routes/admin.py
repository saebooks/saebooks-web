"""Admin views — Lane D cycle 54.

Route map
---------
GET  /admin/sql-tool             — SQL editor + results (HTMX inline)
POST /admin/sql-tool/execute     — execute query, render results fragment
GET  /admin/audit                — paginated audit log with entity_type + date filters
GET  /admin/audit/{snapshot_id}  — single snapshot detail (proxied)

API endpoints consumed:
- GET  /admin/sql          → HTML (we proxy to our own templates instead, using API data)
- POST /admin/sql          → HTML
- GET  /admin/audit        → HTML
  Because the saebooks API serves these as HTML pages (not JSON), the web layer
  exposes its own thin wrappers and calls the relevant JSON endpoints where
  available, or forwards directly.

  For sql-tool: POST /admin/sql (form: sql=...) → HTML with embedded result table.
  For audit:    GET /admin/audit (query params) → HTML page.

  Since the API renders HTML (not JSON), we PROXY the rendered HTML from the
  upstream API response rather than rendering our own templates, keeping a single
  source of truth.  The base.html nav wraps the proxied fragment.

  Alternatively: render our own templates using the API's underlying data.
  We choose to render our own templates for nav consistency.

  Audit JSON API:   GET /api/v1/audit (paginated snapshots list)
  SQL is admin-only; no JSON API — we proxy the form POST and return the raw response.

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


def _is_sae_staff(request: Request) -> bool:
    """True if the session was flagged as SAE staff at login.

    Set by the login handler when the authenticated user matches the
    ``SAE_STAFF_USERNAMES`` allowlist.  Used by the SQL tool routes,
    which bypass tenant RLS and must not be reachable by tenant admins.
    """
    return bool(request.session.get("is_sae_staff"))


def _is_admin(request: Request) -> bool:
    role = request.session.get("user_role", "")
    return _is_sae_staff(request) or role == "admin"


# ---------------------------------------------------------------------------
# SQL Tool — GET /admin/sql-tool
# ---------------------------------------------------------------------------


@router.get("/admin/sql-tool", response_class=HTMLResponse, response_model=None)
async def sql_tool_index(
    request: Request,
    q: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the SQL tool editor.

    On initial load renders an empty editor.  The ``q`` query parameter
    can pre-fill the textarea (e.g. from a history link).
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_sae_staff(request):
        return HTMLResponse("Forbidden — SAE staff only", status_code=403)

    return _TEMPLATES.TemplateResponse(
        request,
        "admin/sql_tool.html",
        {
            "sql": q or "",
            "result": None,
            "error": None,
        },
    )


# ---------------------------------------------------------------------------
# SQL Tool — POST /admin/sql-tool/execute
# ---------------------------------------------------------------------------


@router.post("/admin/sql-tool/execute", response_class=HTMLResponse, response_model=None)
async def sql_tool_execute(request: Request) -> HTMLResponse | RedirectResponse:
    """Execute a SQL query via the API and render the results inline (HTMX).

    Proxies ``POST /admin/sql`` (form: ``sql=...``) to the upstream API and
    parses the rendered result for inline display.  Returns the
    ``admin/sql_tool_results.html`` fragment for HTMX swapping.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_sae_staff(request):
        return HTMLResponse("Forbidden — SAE staff only", status_code=403)

    form_data = await request.form()
    sql = str(form_data.get("sql", "")).strip()

    result_rows: list[dict] = []
    columns: list[str] = []
    error: str | None = None
    truncated: bool = False

    async with api_client(request) as client:
        resp = await client.post("/admin/sql", data={"sql": sql})

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        # The API renders an HTML page with the results table embedded.
        # We return a fresh results fragment — the API doesn't expose a JSON
        # endpoint for sql queries.  Parse the plain text result from the
        # upstream response body to extract the table data is fragile;
        # instead we proxy the result JSON via a dedicated form submit to
        # /admin/sql which returns HTML.  We capture status only.
        # Since the upstream returns HTML, we embed it directly in our
        # results template as a passthrough iframe-style block.
        # Simpler: trust the upstream 200 and render a "query ran" message.
        # Full implementation proxies the result table.
        proxy_html = resp.text
        return _TEMPLATES.TemplateResponse(
            request,
            "admin/sql_tool_results.html",
            {
                "sql": sql,
                "proxy_html": proxy_html,
                "columns": columns,
                "rows": result_rows,
                "error": error,
                "truncated": truncated,
            },
        )

    # Error response
    try:
        detail = resp.json().get("detail", f"Query failed: HTTP {resp.status_code}")
    except Exception:
        detail = f"Query failed: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "admin/sql_tool_results.html",
        {
            "sql": sql,
            "proxy_html": None,
            "columns": columns,
            "rows": result_rows,
            "error": str(detail),
            "truncated": truncated,
        },
        status_code=200,  # always 200 for HTMX — error shown inline
    )


# ---------------------------------------------------------------------------
# Audit Log — GET /admin/audit
# ---------------------------------------------------------------------------


@router.get("/admin/audit", response_class=HTMLResponse, response_model=None)
async def audit_log(
    request: Request,
    entity_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
) -> HTMLResponse | RedirectResponse:
    """Render the paginated audit log list.

    Calls ``GET /api/v1/admin/audit-log`` (JSON) on the upstream API and
    renders rows in our own template.

    The upstream returns ``AuditLogEntry`` rows ({id, entity, entity_id,
    op, actor, at, version, payload}) — we translate them to the field
    names the template was written against ({id, table_name, row_id,
    action, performed_by, performed_at}).

    SAE staff only — cross-tenant data.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_sae_staff(request):
        return HTMLResponse("Forbidden — SAE staff only", status_code=403)

    page_size = 50
    offset = max(0, (page - 1) * page_size)
    params: dict[str, object] = {"limit": page_size, "offset": offset}
    if entity_type:
        params["route"] = entity_type
    # API expects ISO datetimes; date strings are accepted as date-only ISO.
    if date_from:
        params["from_ts"] = date_from
    if date_to:
        params["to_ts"] = date_to

    snapshots: list[dict] = []
    has_next = False
    error: str | None = None
    entity_types: list[str] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/admin/audit-log", params=params)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        data = resp.json()
        items = data.get("items", [])
        total = int(data.get("total", 0) or 0)
        has_next = (offset + len(items)) < total
        # Translate API field names → template field names.
        for r in items:
            payload = r.get("payload") or {}
            snapshots.append(
                {
                    "id": r.get("id"),
                    "table_name": r.get("entity") or "—",
                    "row_id": str(r.get("entity_id") or ""),
                    "action": (r.get("op") or "").upper() or "—",
                    "performed_by": r.get("actor") or payload.get("user_id") or "system",
                    "performed_at": r.get("at"),
                }
            )
        # Distinct entity names visible on this page — populates the filter
        # dropdown so the user has at least the current page's tables to pick
        # from. Cheap and good enough; a dedicated /entities endpoint would
        # be a future enhancement.
        entity_types = sorted({s["table_name"] for s in snapshots if s["table_name"] != "—"})
    else:
        error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "admin/audit_log.html",
        {
            "snapshots": snapshots,
            "has_next": has_next,
            "page": page,
            "page_size": page_size,
            "entity_types": entity_types,
            "filters": {
                "entity_type": entity_type or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# License — GET /admin/license
# ---------------------------------------------------------------------------


@router.get("/admin/license", response_class=HTMLResponse, response_model=None)
async def license_page(request: Request) -> HTMLResponse | RedirectResponse:
    """Show the active edition and per-feature flag matrix."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    edition: str = "community"
    flags: dict[str, bool] = {}
    all_flags: list[str] = []
    tier_order: list[str] = []
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/api/v1/license")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        data = resp.json()
        edition = data.get("edition", "community")
        flags = data.get("flags", {})
        all_flags = data.get("all_flags", list(flags.keys()))
        tier_order = data.get("tier_order", [])
    else:
        error = f"API error: HTTP {resp.status_code}"

    return _TEMPLATES.TemplateResponse(
        request,
        "admin/license.html",
        {
            "edition": edition,
            "flags": flags,
            "all_flags": all_flags,
            "tier_order": tier_order,
            "error": error,
        },
    )
