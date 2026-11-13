"""Year-end close checklist fragment (gap P2-3, 2026-11-13 sweep).

``GET /reports/close-year/checklist?through=…&from_date=…`` renders what is
still open inside the period being closed — drafts, bank lines not
reconciled, depreciation not run, draft pay runs — from the engine's
``GET /api/v1/attention/year-end``, each with a link to the list that shows
them. When anything is open it also places a required "I have checked this
list" tick into the close form (out-of-band swap into ``#close-ack-slot``),
so the close cannot be posted by accident with work still open. The close
itself is unchanged (``POST /reports/close-year``).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)


def checklist_lines(payload: dict, through: str, from_date: str | None) -> list[dict]:
    rng = f"&date_to={through}" + (f"&date_from={from_date}" if from_date else "")
    meta = {
        "draft_invoices": (_("Draft invoices dated in the period"), f"/invoices?status=draft{rng}"),
        "draft_bills": (_("Draft bills dated in the period"), f"/bills?status=draft{rng}"),
        "draft_expenses": (_("Draft expenses dated in the period"), f"/expenses?status=DRAFT{rng}"),
        "draft_journals": (_("Draft journal entries dated in the period"), "/journal-entries?status=DRAFT"),
        "bank_lines_to_reconcile": (_("Bank lines not yet reconciled"), "/reconciliation"),
        "depreciation_not_run": (_("Assets with depreciation not run to this date"), "/fixed-assets"),
        "draft_pay_runs": (_("Pay runs not finalised"), "/wages"),
    }
    lines = []
    for item in payload.get("items", []):
        key = item.get("key")
        if key not in meta:
            continue
        label, href = meta[key]
        lines.append({
            "key": key, "label": label, "href": href,
            "count": int(item.get("count") or 0), "oldest": item.get("oldest"),
        })
    return lines


def _year_before(through: str) -> str | None:
    """The day after ``through`` one year earlier (2026-06-30 -> 2025-07-01)."""
    from datetime import date, timedelta

    try:
        end = date.fromisoformat(through)
    except ValueError:
        return None
    try:
        start = end.replace(year=end.year - 1)
    except ValueError:  # 29 February
        start = end.replace(year=end.year - 1, day=28)
    return (start + timedelta(days=1)).isoformat()


@router.get("/reports/close-year/checklist", response_class=HTMLResponse, response_model=None)
async def close_checklist(
    request: Request, through: str = "", from_date: str | None = None
) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    payload: dict = {}
    failed = False
    if through and not from_date:
        # Default the period to the twelve months ending on ``through``.
        from_date = _year_before(through)
    if through:
        params = {"through_date": through}
        if from_date:
            params["from_date"] = from_date
        try:
            async with api_client(request) as client:
                resp = await client.get("/api/v1/attention/year-end", params=params)
            if resp.is_success:
                payload = resp.json()
            else:
                failed = True
        except ModuleUnavailable:
            failed = True
    else:
        failed = True
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/close_checklist.html",
        {
            "lines": checklist_lines(payload, through, from_date),
            "ready": bool(payload.get("ready")),
            "already_locked": bool(payload.get("already_locked")),
            "locked_through": payload.get("locked_through"),
            "failed": failed,
            "through": through,
        },
    )
