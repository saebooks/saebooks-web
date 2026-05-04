"""Pay Run views — Cat-C rewrite to /api/v1/pay-runs/*.

Route map (URL paths unchanged for the customer)
---------
GET  /pay-run           — list draft/recent pay runs + form to create one.
POST /pay-run/new       — create a new draft pay run (POST, redirect to /pay-run/{id}).
GET  /pay-run/{id}      — detail: lines, export-aba, finalize controls.
POST /pay-run/{id}/lines          — add an employee line (HTMX fragment).
POST /pay-run/{id}/lines/{lid}/delete — remove a line (HTMX fragment).
POST /pay-run/{id}/export-aba     — generate ABA file, trigger download.
POST /pay-run/{id}/finalize       — finalize the pay run.

API endpoints consumed (all /api/v1/pay-runs/*)
- GET  /api/v1/pay-runs                     list
- POST /api/v1/pay-runs                     create draft
- GET  /api/v1/pay-runs/{id}               fetch with lines
- POST /api/v1/pay-runs/{id}/lines          add line
- DELETE /api/v1/pay-runs/{id}/lines/{lid}  remove line
- POST /api/v1/pay-runs/{id}/export-aba     export ABA + journal
- PUT  /api/v1/pay-runs/{id}/finalize       finalize

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import base64
from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# GET /pay-run — list recent pay runs
# ---------------------------------------------------------------------------


@router.get("/pay-run", response_class=HTMLResponse, response_model=None)
async def pay_run_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the pay-run index: recent runs + a create form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    pay_runs: list[dict] = []
    error: str | None = None

    async with api_client(request) as client:
        r = await client.get(
            "/api/v1/pay-runs",
            params={"limit": 50, "offset": 0},
        )
        if r.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if r.is_success:
            pay_runs = r.json().get("items", [])
        else:
            error = f"API error fetching pay runs: HTTP {r.status_code}"

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "pay_run/index.html",
        {
            "pay_runs": pay_runs,
            "today": date.today().isoformat(),
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /pay-run/new — create draft pay run
# ---------------------------------------------------------------------------


@router.post("/pay-run/new", response_model=None)
async def pay_run_new(request: Request) -> RedirectResponse:
    """Create a new draft pay run; redirect to its detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    payload = {
        "period_start": str(form.get("period_start", "")),
        "period_end": str(form.get("period_end", "")),
        "payment_date": str(form.get("payment_date", "")),
        "description": str(form.get("description", "") or ""),
    }

    async with api_client(request) as client:
        r = await client.post("/api/v1/pay-runs", json=payload)

    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if r.is_success:
        pr_id = r.json()["id"]
        return RedirectResponse(url=f"/pay-run/{pr_id}", status_code=303)

    try:
        detail = r.json().get("detail", f"Create failed: HTTP {r.status_code}")
    except Exception:
        detail = f"Create failed: HTTP {r.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url="/pay-run", status_code=303)


# ---------------------------------------------------------------------------
# GET /pay-run/{id} — detail view
# ---------------------------------------------------------------------------


@router.get("/pay-run/{pay_run_id}", response_class=HTMLResponse, response_model=None)
async def pay_run_detail(pay_run_id: UUID, request: Request) -> HTMLResponse | RedirectResponse:
    """Render a pay run's detail page with lines and action buttons."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    pay_run: dict | None = None
    error: str | None = None

    async with api_client(request) as client:
        r = await client.get(f"/api/v1/pay-runs/{pay_run_id}")
        if r.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if r.status_code == 404:
            error = "Pay run not found"
        elif r.is_success:
            pay_run = r.json()
        else:
            error = f"API error: HTTP {r.status_code}"

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "pay_run/detail.html",
        {
            "pay_run": pay_run,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /pay-run/{id}/lines — add line
# ---------------------------------------------------------------------------


@router.post("/pay-run/{pay_run_id}/lines", response_model=None)
async def pay_run_add_line(
    pay_run_id: UUID, request: Request
) -> RedirectResponse:
    """Add a line to the pay run; redirect back to detail."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    payload = {
        "employee_id": str(form.get("employee_id", "")),
        "gross": str(form.get("gross", "0")),
        "tax": str(form.get("tax", "0")),
        "super_amount": str(form.get("super_amount", "0")),
        "net": str(form.get("net", "0")),
    }

    async with api_client(request) as client:
        r = await client.post(
            f"/api/v1/pay-runs/{pay_run_id}/lines",
            json=payload,
        )

    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if not r.is_success:
        try:
            detail = r.json().get("detail", f"Add line failed: HTTP {r.status_code}")
        except Exception:
            detail = f"Add line failed: HTTP {r.status_code}"
        request.session["flash"] = str(detail)

    return RedirectResponse(url=f"/pay-run/{pay_run_id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /pay-run/{id}/lines/{lid}/delete — remove line
# ---------------------------------------------------------------------------


@router.post(
    "/pay-run/{pay_run_id}/lines/{line_id}/delete", response_model=None
)
async def pay_run_delete_line(
    pay_run_id: UUID, line_id: UUID, request: Request
) -> RedirectResponse:
    """Remove a pay-run line; redirect back to detail."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        r = await client.delete(
            f"/api/v1/pay-runs/{pay_run_id}/lines/{line_id}"
        )

    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if not r.is_success and r.status_code != 204:
        try:
            detail = r.json().get("detail", f"Delete failed: HTTP {r.status_code}")
        except Exception:
            detail = f"Delete failed: HTTP {r.status_code}"
        request.session["flash"] = str(detail)

    return RedirectResponse(url=f"/pay-run/{pay_run_id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /pay-run/{id}/export-aba — generate + download ABA file
# ---------------------------------------------------------------------------


@router.post("/pay-run/{pay_run_id}/export-aba", response_model=None)
async def pay_run_export_aba(
    pay_run_id: UUID, request: Request
) -> Response | RedirectResponse:
    """Generate the ABA file; return it as a download.

    Reads the current version from the API (GET pay-run/{id}) and passes
    it as the If-Match header so optimistic locking works without requiring
    the template to track the version in a hidden field.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        # Fetch current state to get the version.
        r_get = await client.get(f"/api/v1/pay-runs/{pay_run_id}")
        if r_get.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if not r_get.is_success:
            request.session["flash"] = f"Pay run not found (HTTP {r_get.status_code})"
            return RedirectResponse(url="/pay-run", status_code=303)

        version = str(r_get.json().get("version", 1))
        period_start = r_get.json().get("period_start", "")

        r = await client.post(
            f"/api/v1/pay-runs/{pay_run_id}/export-aba",
            headers={"If-Match": version},
        )

    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if r.is_success:
        body = r.json()
        aba_bytes = base64.b64decode(body["aba_file_b64"])
        date_part = (period_start or "").replace("-", "")[:6] or "000000"
        filename = f"payroll-{date_part}.txt"
        return Response(
            content=aba_bytes,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    try:
        detail = r.json().get("detail", f"Export failed: HTTP {r.status_code}")
    except Exception:
        detail = f"Export failed: HTTP {r.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/pay-run/{pay_run_id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /pay-run/{id}/finalize — finalize the pay run
# ---------------------------------------------------------------------------


@router.post("/pay-run/{pay_run_id}/finalize", response_model=None)
async def pay_run_finalize(
    pay_run_id: UUID, request: Request
) -> RedirectResponse:
    """Finalize the pay run (post its journal).

    Same pattern as export-aba: fetches current version first.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        r_get = await client.get(f"/api/v1/pay-runs/{pay_run_id}")
        if r_get.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if not r_get.is_success:
            request.session["flash"] = f"Pay run not found (HTTP {r_get.status_code})"
            return RedirectResponse(url="/pay-run", status_code=303)

        version = str(r_get.json().get("version", 1))

        r = await client.put(
            f"/api/v1/pay-runs/{pay_run_id}/finalize",
            headers={"If-Match": version},
        )

    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if r.is_success:
        request.session["flash"] = "Pay run finalized. Journal has been posted."
    else:
        try:
            detail = r.json().get("detail", f"Finalize failed: HTTP {r.status_code}")
        except Exception:
            detail = f"Finalize failed: HTTP {r.status_code}"
        request.session["flash"] = str(detail)

    return RedirectResponse(url=f"/pay-run/{pay_run_id}", status_code=303)
