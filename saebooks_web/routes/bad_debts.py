"""Bad-debt write-off & recovery — candidate screen + HTMX write-off action.

Phase 2 / Task 9.

GET  /bad-debts
    List write-off *candidates*: POSTED invoices whose balance (total minus
    amount_paid) is still > 0 and whose age past ``due_date`` exceeds the
    company's ``writeoff_threshold_days``.  The company's ``writeoff_mode``
    decides the surface:
      * review  — each row gets an HTMX "Write off" button (Task 9 happy path)
      * auto    — read-only; a scheduled job (Task 10) does the writing-off,
                  so the screen is a log/preview only
      * manual  — read-only list; the operator writes off elsewhere

POST /bad-debts/{invoice_id}/write-off   (HTMX, review mode)
    Proxy to the engine ``POST /api/v1/invoices/{id}/write-off`` endpoint and
    swap the row out for a "written off" confirmation fragment.  The web app
    NEVER posts the journal entry itself — the engine owns the ledger.

All ledger effects live in the engine; this module only drives policy + UX.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from saebooks_web.api_client import api_client
from saebooks_web.bad_debt_logic import CANDIDATE_STATUS as _CANDIDATE_STATUS

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

# Imported lazily-styled like the other routes: a module-level Jinja2Templates.
from fastapi.templating import Jinja2Templates  # noqa: E402

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

from saebooks_web.bad_debt_logic import candidates as _candidates  # noqa: E402


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")




async def _load_active_company(client) -> dict | None:
    """Resolve the active company (X-Company-Id is injected by api_client)."""
    resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
    if resp.is_success:
        items = resp.json().get("items", [])
        if items:
            return items[0]
    return None


@router.get("/bad-debts", response_class=HTMLResponse, response_model=None)
async def bad_debts_list(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the bad-debt candidate screen for the active company."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    company: dict | None = None
    invoices: list[dict] = []
    contacts_by_id: dict[str, dict] = {}

    async with api_client(request) as client:
        company = await _load_active_company(client)

        # Pull POSTED invoices (single wide page; the API caps page_size at 500).
        resp = await client.get(
            "/api/v1/invoices",
            params={"status": _CANDIDATE_STATUS, "page": 1, "page_size": 500},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            invoices = resp.json().get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

        # Resolve customer names for display.
        for ctype in ("CUSTOMER", "BOTH"):
            c_resp = await client.get(
                "/api/v1/contacts",
                params={"type": ctype, "limit": 500, "offset": 0},
            )
            if c_resp.is_success:
                for c in c_resp.json().get("items", []):
                    contacts_by_id[c["id"]] = c

    writeoff_mode = (company or {}).get("writeoff_mode") or "review"
    threshold_days = int((company or {}).get("writeoff_threshold_days") or 90)
    today = date.today()

    candidates = _candidates(invoices, threshold_days, today)

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "bad_debts/list.html",
        {
            "company": company,
            "writeoff_mode": writeoff_mode,
            "threshold_days": threshold_days,
            "candidates": candidates,
            "contacts_by_id": contacts_by_id,
            "today": today.isoformat(),
            "error": error,
            "flash": flash,
        },
    )


@router.post(
    "/bad-debts/{invoice_id}/write-off",
    response_class=HTMLResponse,
    response_model=None,
)
async def bad_debts_write_off(
    request: Request, invoice_id: str
) -> HTMLResponse | RedirectResponse:
    """HTMX action: write off one invoice via the engine, swap the row out.

    Only meaningful in ``review`` mode, but we don't hard-block other modes —
    the engine is the authority on whether the invoice can be written off
    (409 if already WRITTEN_OFF / nothing owed).  On success we return a
    confirmation fragment that replaces the table row (hx-swap="outerHTML").
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    reason = (form.get("reason") or "").strip() or None

    payload: dict[str, object] = {}
    if reason:
        payload["reason"] = reason

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/invoices/{invoice_id}/write-off",
            json=payload,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        inv = resp.json()
        return _TEMPLATES.TemplateResponse(
            request,
            "bad_debts/_row_written_off.html",
            {"invoice": inv},
        )

    # 404 / 409 / 422 — render an inline error row (keeps the original row
    # visible so the operator can retry or investigate).
    msg = f"Write-off failed (HTTP {resp.status_code})"
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, str):
            msg = detail
    except Exception:
        pass

    return _TEMPLATES.TemplateResponse(
        request,
        "bad_debts/_row_error.html",
        {"invoice_id": invoice_id, "message": msg},
        status_code=resp.status_code if resp.status_code in (404, 409, 422) else 502,
    )
