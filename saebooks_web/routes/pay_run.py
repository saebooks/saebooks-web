"""Pay Run views — Lane D cycle 54.

Route map
---------
GET  /pay-run           — list of POSTED bills with balance_due > 0, bank-account picker,
                          process-date picker.
POST /pay-run/export    — submit selections, proxy to API, return ABA file download.

API endpoints consumed:
- GET  /pay-run                 → {"candidates": [...], "bank_accounts": [...], "today": ...}
  Actually: the saebooks API exposes /pay-run as an HTML route. We instead call the
  underlying bill and bank-account list endpoints to build our own picker.

  Candidate bills:    GET /api/v1/bills?status=POSTED&limit=200
  Bank accounts:      GET /api/v1/bank-accounts?has_aba=true&limit=200
  Export:             POST /pay-run/export (form-encoded, returns text/plain ABA file)

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

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
# GET /pay-run
# ---------------------------------------------------------------------------


@router.get("/pay-run", response_class=HTMLResponse, response_model=None)
async def pay_run_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the pay-run picker.

    Fetches POSTED bills with balance_due > 0 and bank accounts with ABA fields
    populated from the API to build the selection table.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    candidates: list[dict] = []
    bank_accounts: list[dict] = []
    error: str | None = None

    async with api_client(request) as client:
        bills_resp = await client.get(
            "/api/v1/bills",
            params={"status": "POSTED", "limit": 200, "offset": 0},
        )
        if bills_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if bills_resp.is_success:
            all_bills = bills_resp.json().get("items", [])
            # Filter to bills that still have an outstanding balance.
            candidates = [
                b for b in all_bills
                if float(b.get("balance_due") or b.get("amount_due") or 0) > 0
            ]
        else:
            error = f"API error fetching bills: HTTP {bills_resp.status_code}"

        ba_resp = await client.get(
            "/api/v1/bank-accounts",
            params={"limit": 200, "offset": 0},
        )
        if ba_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if ba_resp.is_success:
            payload = ba_resp.json()
            all_ba = payload.get("items", payload) if isinstance(payload, dict) else payload
            # Only include accounts that have BSB + APCA user ID for ABA generation.
            bank_accounts = [
                a for a in all_ba
                if a.get("bsb") and a.get("apca_user_id")
            ]
        elif error is None:
            error = f"API error fetching bank accounts: HTTP {ba_resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "pay_run/index.html",
        {
            "candidates": candidates,
            "bank_accounts": bank_accounts,
            "today": date.today().isoformat(),
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /pay-run/export
# ---------------------------------------------------------------------------


@router.post("/pay-run/export", response_model=None)
async def pay_run_export(request: Request) -> Response | RedirectResponse:
    """Submit pay-run selections and proxy the ABA file download from the API.

    Forwards the raw form data (``bank_account_id``, ``process_date``,
    ``description``, ``select_<id>``, ``amount_<id>``) directly to the
    upstream ``POST /pay-run/export`` endpoint.  On success the ABA text
    is returned as a download.  On error redirects to /pay-run with a flash.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    # Re-encode as a plain dict for forwarding.
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}  # type: ignore[misc]

    async with api_client(request) as client:
        resp = await client.post("/pay-run/export", data=form)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        # Pass through the ABA file — preserve Content-Disposition from upstream.
        content_disposition = resp.headers.get(
            "content-disposition",
            'attachment; filename="pay-run.txt"',
        )
        return Response(
            content=resp.content,
            media_type="text/plain",
            headers={"Content-Disposition": content_disposition},
        )

    # Error — extract detail and redirect back with flash.
    try:
        detail = resp.json().get("detail", f"Export failed: HTTP {resp.status_code}")
    except Exception:
        detail = f"Export failed: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url="/pay-run", status_code=303)
