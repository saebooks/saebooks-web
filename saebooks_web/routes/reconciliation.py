"""Bank reconciliation — the daily loop (Lane D cycle 49; rebuilt batch 4,
2026-11-13).

Route map
---------
GET  /reconciliation                                — accounts picker with balances
GET  /reconciliation/statements/{rec_id}            — reconciliation report (printable)
GET  /reconciliation/statements/{rec_id}/report.csv — report as a spreadsheet file
POST /reconciliation/statements/{rec_id}/finish     — Finish (difference must be nil)
POST /reconciliation/statements/{rec_id}/reopen     — reopen the latest finished one
POST /reconciliation/statements/{rec_id}/cancel     — throw away one in progress
POST /reconciliation/match                          — match a line to one journal entry
GET  /reconciliation/{account_id}/lines             — the reconcile screen (tabs)
GET  /reconciliation/{account_id}/difference        — live difference (HTMX partial)
POST /reconciliation/{account_id}/statement         — start / update the statement
GET  /reconciliation/{account_id}/history           — past reconciliations
POST /reconciliation/{account_id}/auto-match        — auto-match the account
GET  /reconciliation/{bsl_id}/suggest               — exact-amount suggestions
GET  /reconciliation/{bsl_id}/find                  — match picker (search)
POST /reconciliation/{bsl_id}/match-many            — match to one or more records
GET  /reconciliation/{bsl_id}/create                — create a record from the line
POST /reconciliation/{bsl_id}/create
GET  /reconciliation/{bsl_id}/split                 — split the line across records
POST /reconciliation/{bsl_id}/split
POST /reconciliation/{bsl_id}/ignore                — set a line aside
POST /reconciliation/{bsl_id}/unignore              — put it back
POST /reconciliation/{bsl_id}/accept-rule           — use the suggested bank rule
GET  /reconciliation/{bsl_id}/unmatch               — confirm page (void created?)
POST /reconciliation/{bsl_id}/unmatch               — unmatch

Every POST redirects back with a flash message; the engine's plain-words
error ``detail`` is shown as-is. Degrade (M2): GET routes catch
ModuleUnavailable and render the degraded panel; POSTs flash and redirect.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_LINES_PAGE_SIZE = 50
_TABS = {"to_match": "TO_MATCH", "matched": "MATCHED", "ignored": "IGNORED"}
_SPLIT_ROWS = 6


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _api_error(status_code: int) -> str:
    return _("The reconciliation data could not be loaded (HTTP %(code)s).") % {
        "code": status_code
    }


def _engine_down() -> str:
    return _(
        "The accounting engine could not be reached — nothing was changed. Try again in a moment."
    )


def _detail(resp: Any, fallback: str) -> str:
    try:
        d = resp.json().get("detail", fallback)
    except Exception:
        return fallback
    if isinstance(d, list):  # pydantic validation errors
        return "; ".join(str(x.get("msg", x)) for x in d) or fallback
    return str(d)


def _lines_url(account_id: str, tab: str | None = None) -> str:
    if not account_id:
        return "/reconciliation"
    url = f"/reconciliation/{account_id}/lines"
    return f"{url}?tab={tab}" if tab else url


def _dec(value: Any) -> Decimal | None:
    try:
        s = str(value or "").strip().replace(",", "").replace("$", "")
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


async def _accounts(client: Any) -> list[dict]:
    resp = await client.get("/api/v1/reconciliation/accounts")
    if resp.is_success:
        payload = resp.json()
        return payload if isinstance(payload, list) else payload.get("items", [])
    return []


async def _line(client: Any, bsl_id: str) -> dict | None:
    resp = await client.get(f"/api/v1/bank_statement_lines/{bsl_id}")
    return resp.json() if resp.is_success else None


async def _remaining_on_line(client: Any, line: dict) -> Decimal:
    """What is still unmatched on a line (amount minus live allocations)."""
    resp = await client.get(f"/api/v1/reconciliation/line_detail/{line['id']}")
    if resp.is_success:
        return Decimal(str(resp.json().get("remaining", line["amount"])))
    return Decimal(str(line["amount"]))


# ---------------------------------------------------------------------------
# Accounts picker — GET /reconciliation
# ---------------------------------------------------------------------------


@router.get("/reconciliation", response_class=HTMLResponse, response_model=None)
async def reconciliation_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the accounts picker — reconcilable accounts with balances."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    accounts: list[dict] = []
    degraded = False

    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/reconciliation/accounts")

            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)

            if resp.is_success:
                payload = resp.json()
                accounts = payload if isinstance(payload, list) else payload.get("items", [])
            else:
                error = _api_error(resp.status_code)
    except ModuleUnavailable:
        degraded = True

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/index.html",
        {"accounts": accounts, "error": error, "degraded": degraded, "flash": flash},
    )


# ---------------------------------------------------------------------------
# Reconciliation records — report, finish, reopen, cancel
# NOTE: declared before the /{id}/... routes.
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/statements/{rec_id}",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_report(request: Request, rec_id: str) -> HTMLResponse | RedirectResponse:
    """The reconciliation report — printable; frozen once finished."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    report: dict | None = None
    error: str | None = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/reconciliation/statements/{rec_id}")
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                report = resp.json()
            else:
                error = _detail(resp, _api_error(resp.status_code))
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/report.html",
        {
            "report": report,
            "error": error,
            "degraded": degraded,
            "flash": request.session.pop("flash", None),
        },
        status_code=200 if report or degraded else 404,
    )


@router.get("/reconciliation/statements/{rec_id}/report.csv", response_model=None)
async def reconciliation_report_csv(request: Request, rec_id: str) -> Response:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/reconciliation/statements/{rec_id}/report.csv")
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=f"/reconciliation/statements/{rec_id}", status_code=303)
    if not resp.is_success:
        request.session["flash"] = _detail(resp, _api_error(resp.status_code))
        return RedirectResponse(url=f"/reconciliation/statements/{rec_id}", status_code=303)
    return Response(
        content=resp.content,
        media_type="text/csv",
        headers={
            "Content-Disposition": resp.headers.get(
                "content-disposition", 'attachment; filename="bank-reconciliation.csv"'
            )
        },
    )


async def _rec_post(
    request: Request, rec_id: str, action: str, body: dict | None, ok: str
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    back = str(form.get("next", "")).strip() or f"/reconciliation/statements/{rec_id}"
    if not back.startswith("/reconciliation"):
        back = f"/reconciliation/statements/{rec_id}"
    try:
        async with api_client(request) as client:
            if action == "cancel":
                resp = await client.delete(f"/api/v1/reconciliation/statements/{rec_id}")
            else:
                resp = await client.post(
                    f"/api/v1/reconciliation/statements/{rec_id}/{action}",
                    json=body if body is not None else None,
                )
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=back, status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    request.session["flash"] = ok if resp.is_success else _detail(
        resp, _("That did not work (HTTP %(code)s).") % {"code": resp.status_code}
    )
    return RedirectResponse(url=back, status_code=303)


@router.post("/reconciliation/statements/{rec_id}/finish", response_model=None)
async def reconciliation_finish(request: Request, rec_id: str) -> RedirectResponse:
    return await _rec_post(
        request, rec_id, "finish", None,
        _("Reconciliation finished. The lines it covers are now locked."),
    )


@router.post("/reconciliation/statements/{rec_id}/reopen", response_model=None)
async def reconciliation_reopen(request: Request, rec_id: str) -> RedirectResponse:
    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    return await _rec_post(
        request, rec_id, "reopen", {"reason": reason},
        _("Reconciliation reopened. Its lines can be changed again."),
    )


@router.post("/reconciliation/statements/{rec_id}/cancel", response_model=None)
async def reconciliation_cancel(request: Request, rec_id: str) -> RedirectResponse:
    return await _rec_post(request, rec_id, "cancel", None, _("Reconciliation cancelled."))


# ---------------------------------------------------------------------------
# The reconcile screen — GET /reconciliation/{account_id}/lines
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{account_id}/lines",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_lines(
    request: Request,
    account_id: str,
    tab: str = "to_match",
    offset: int = 0,
    limit: int = _LINES_PAGE_SIZE,
) -> HTMLResponse | RedirectResponse:
    """The reconcile screen: statement panel + To match / Matched / Ignored."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    tab = tab if tab in _TABS else "to_match"
    offset = max(offset, 0)
    limit = limit if 0 < limit <= 200 else _LINES_PAGE_SIZE
    error: str | None = None
    lines: list[dict] = []
    counts: dict = {}
    total = 0
    account: dict | None = None
    accounts: list[dict] = []
    report: dict | None = None
    history: list[dict] = []
    degraded = False

    try:
        async with api_client(request) as client:
            acct_resp = await client.get("/api/v1/reconciliation/accounts")
            if acct_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if acct_resp.is_success:
                payload = acct_resp.json()
                accounts = payload if isinstance(payload, list) else payload.get("items", [])
                account = next((a for a in accounts if a.get("id") == account_id), None)

            resp = await client.get(
                "/api/v1/reconciliation/lines",
                params={"account_id": account_id, "status": _TABS[tab],
                        "limit": limit, "offset": offset},
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                body = resp.json()
                lines = body.get("items", [])
                counts = body.get("counts", {})
                total = body.get("total", len(lines))
            else:
                error = _api_error(resp.status_code)

            hist_resp = await client.get(
                "/api/v1/reconciliation/statements", params={"account_id": account_id}
            )
            if hist_resp.is_success:
                history = hist_resp.json().get("items", [])
            open_rec = next((r for r in history if r.get("status") == "OPEN"), None)
            if open_rec:
                rep = await client.get(f"/api/v1/reconciliation/statements/{open_rec['id']}")
                if rep.is_success:
                    report = rep.json()
    except ModuleUnavailable:
        degraded = True

    last_finished = next((r for r in history if r.get("status") == "FINISHED"), None)
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/lines.html",
        {
            "account_id": account_id,
            "account": account,
            "other_accounts": [a for a in accounts if a.get("id") != account_id],
            "lines": lines,
            "counts": counts,
            "tab": tab,
            "all_lines_total": total,
            "offset": offset,
            "limit": limit,
            "report": report,
            "last_finished": last_finished,
            "error": error,
            "degraded": degraded,
            "flash": request.session.pop("flash", None),
        },
    )


@router.get(
    "/reconciliation/{account_id}/difference",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_difference(
    request: Request,
    account_id: str,
    statement_date: str = "",
    statement_balance: str = "",
) -> HTMLResponse:
    """HTMX partial: the live difference as the user types the balance."""
    if not _require_auth(request):
        return HTMLResponse("", status_code=401)
    preview: dict | None = None
    message: str | None = None
    bal = _dec(statement_balance)
    if not statement_date or bal is None:
        message = _("Enter the statement date and closing balance.")
    else:
        try:
            async with api_client(request) as client:
                resp = await client.get(
                    "/api/v1/reconciliation/preview",
                    params={"account_id": account_id, "statement_date": statement_date,
                            "statement_balance": str(bal)},
                )
            if resp.is_success:
                preview = resp.json()
            else:
                message = _detail(resp, _api_error(resp.status_code))
        except ModuleUnavailable:
            message = _engine_down()
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/_difference.html",
        {"preview": preview, "message": message},
    )


@router.post("/reconciliation/{account_id}/statement", response_model=None)
async def reconciliation_statement(request: Request, account_id: str) -> RedirectResponse:
    """Start the reconciliation, or update the one in progress."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    bal = _dec(form.get("statement_balance"))
    sdate = str(form.get("statement_date", "")).strip()
    back = _lines_url(account_id)
    if bal is None or not sdate:
        request.session["flash"] = _("Enter the statement date and the closing balance.")
        return RedirectResponse(url=back, status_code=303)
    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/reconciliation/statements",
                json={"account_id": account_id, "statement_date": sdate,
                      "statement_balance": str(bal)},
            )
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=back, status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    request.session["flash"] = (
        _("Statement saved.") if resp.is_success
        else _detail(resp, _api_error(resp.status_code))
    )
    return RedirectResponse(url=back, status_code=303)


@router.get(
    "/reconciliation/{account_id}/history",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_history(request: Request, account_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items: list[dict] = []
    account: dict | None = None
    error: str | None = None
    degraded = False
    try:
        async with api_client(request) as client:
            account = next(
                (a for a in await _accounts(client) if a.get("id") == account_id), None
            )
            resp = await client.get(
                "/api/v1/reconciliation/statements", params={"account_id": account_id}
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                items = resp.json().get("items", [])
            else:
                error = _detail(resp, _api_error(resp.status_code))
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/history.html",
        {"account_id": account_id, "account": account, "items": items,
         "error": error, "degraded": degraded},
    )


# ---------------------------------------------------------------------------
# Match — POST /reconciliation/match
# ---------------------------------------------------------------------------


@router.post("/reconciliation/match", response_class=HTMLResponse, response_model=None)
async def reconciliation_match(request: Request) -> RedirectResponse:
    """Match a BSL to one journal entry (from the suggestions page)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    bsl_id = str(form_data.get("bsl_id", "")).strip()
    entry_id = str(form_data.get("entry_id", "")).strip()
    account_id = str(form_data.get("account_id", "")).strip()

    payload = {"bsl_id": bsl_id, "entry_id": entry_id}
    redirect_url = _lines_url(account_id)

    try:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/reconciliation/match", json=payload)
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=redirect_url, status_code=303)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = _("Line matched.")
    else:
        request.session["flash"] = _detail(
            resp, _("Match failed (HTTP %(code)s).") % {"code": resp.status_code}
        )

    return RedirectResponse(url=redirect_url, status_code=303)


# ---------------------------------------------------------------------------
# Unmatch — confirm page + POST
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{bsl_id}/unmatch",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_unmatch_confirm(
    request: Request, bsl_id: str, account_id: str = ""
) -> HTMLResponse | RedirectResponse:
    """Confirm an unmatch: lists records created from the line and offers
    to void them so matching again cannot double-book (P1-22)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    preview: dict | None = None
    error: str | None = None
    degraded = False
    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/reconciliation/unmatch_preview/{bsl_id}")
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                preview = resp.json()
            else:
                error = _detail(resp, _api_error(resp.status_code))
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/unmatch.html",
        {"bsl_id": bsl_id, "account_id": account_id, "preview": preview,
         "error": error, "degraded": degraded},
    )


@router.post(
    "/reconciliation/{bsl_id}/unmatch",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_unmatch(request: Request, bsl_id: str) -> RedirectResponse:
    """Remove the matches from a line; optionally void what it created."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    account_id = str(form_data.get("account_id", "")).strip()
    void_created = str(form_data.get("void_created", "")).lower() in ("1", "on", "true", "yes")
    redirect_url = _lines_url(account_id, "matched" if account_id else None)

    try:
        async with api_client(request) as client:
            if void_created:
                resp = await client.post(
                    f"/api/v1/reconciliation/unmatch/{bsl_id}", json={"void_created": True}
                )
            else:
                resp = await client.post(f"/api/v1/reconciliation/unmatch/{bsl_id}")
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=redirect_url, status_code=303)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        voided = []
        try:
            voided = resp.json().get("voided") or []
        except Exception:
            voided = []
        if voided:
            request.session["flash"] = _("Line unmatched. Voided: %(what)s.") % {
                "what": ", ".join(v.get("label", "") for v in voided)
            }
        else:
            request.session["flash"] = _("Line unmatched.")
    else:
        request.session["flash"] = _detail(
            resp, _("Unmatch failed (HTTP %(code)s).") % {"code": resp.status_code}
        )

    return RedirectResponse(url=redirect_url, status_code=303)


# ---------------------------------------------------------------------------
# Suggest — GET /reconciliation/{bsl_id}/suggest
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{bsl_id}/suggest",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_suggest(
    request: Request,
    bsl_id: str,
    account_id: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Exact-amount suggestions for one line, with amount and contact."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    suggestions: list[dict] = []
    degraded = False

    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/reconciliation/suggest/{bsl_id}")

            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)

            if resp.is_success:
                payload = resp.json()
                suggestions = payload if isinstance(payload, list) else payload.get("items", [])
            else:
                error = _api_error(resp.status_code)
    except ModuleUnavailable:
        degraded = True

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/suggest.html",
        {
            "bsl_id": bsl_id,
            "account_id": account_id or "",
            "suggestions": suggestions,
            "error": error,
            "degraded": degraded,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Match picker — GET /reconciliation/{bsl_id}/find, POST /match-many
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{bsl_id}/find",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_find(
    request: Request,
    bsl_id: str,
    account_id: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    amount: str = "",
) -> HTMLResponse | RedirectResponse:
    """Search payments, expenses, money received, transfers and journals on
    this account that are not yet cleared; tick one or several to match."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items: list[dict] = []
    line: dict | None = None
    error: str | None = None
    degraded = False
    params: dict[str, str] = {}
    if q.strip():
        params["q"] = q.strip()
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if _dec(amount) is not None:
        params["amount"] = str(abs(_dec(amount)))  # type: ignore[arg-type]
    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/reconciliation/candidates/{bsl_id}", params=params)
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                body = resp.json()
                items = body.get("items", [])
                line = body.get("line")
            else:
                error = _detail(resp, _api_error(resp.status_code))
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/find.html",
        {
            "bsl_id": bsl_id,
            "account_id": account_id or (line or {}).get("account_id", ""),
            "line": line,
            "items": items,
            "q": q, "date_from": date_from, "date_to": date_to, "amount": amount,
            "error": error,
            "degraded": degraded,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/reconciliation/{bsl_id}/match-many", response_model=None)
async def reconciliation_match_many(request: Request, bsl_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    account_id = str(form.get("account_id", "")).strip()
    picked = [str(v) for v in form.getlist("pick")]
    back_find = f"/reconciliation/{bsl_id}/find?account_id={account_id}"
    if not picked:
        request.session["flash"] = _("Tick at least one record to match.")
        return RedirectResponse(url=back_find, status_code=303)
    items = []
    for tid in picked:
        amt = _dec(form.get(f"amount_{tid}"))
        item: dict[str, Any] = {"target_type": "JOURNAL_ENTRY", "target_id": tid}
        if amt is not None:
            item["amount"] = str(abs(amt))
        items.append(item)
    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/reconciliation/match_many", json={"bsl_id": bsl_id, "items": items}
            )
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=back_find, status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        request.session["flash"] = _("Line matched.")
        return RedirectResponse(url=_lines_url(account_id), status_code=303)
    request.session["flash"] = _detail(resp, _("Match failed (HTTP %(code)s).") % {"code": resp.status_code})
    return RedirectResponse(url=back_find, status_code=303)


# ---------------------------------------------------------------------------
# Create from a line — GET/POST /reconciliation/{bsl_id}/create
# ---------------------------------------------------------------------------

_KINDS_OUT = ("expense", "bill_payment", "transfer")
_KINDS_IN = ("receipt", "customer_payment", "transfer")


async def _form_options(client: Any, *, money_out: bool) -> dict[str, list[dict]]:
    """Contacts, accounts and GST codes for the create and split forms."""
    contacts: list[dict] = []
    for ctype in (("SUPPLIER", "BOTH") if money_out else ("CUSTOMER", "BOTH")):
        r = await client.get("/api/v1/contacts", params={"type": ctype, "limit": 500, "offset": 0})
        if r.is_success:
            contacts.extend(r.json().get("items", []))
    seen: set[str] = set()
    contacts = [c for c in contacts if not (c["id"] in seen or seen.add(c["id"]))]
    contacts.sort(key=lambda c: (c.get("name") or "").lower())
    gl: list[dict] = []
    types = (
        ("EXPENSE", "COST_OF_SALES", "OTHER_EXPENSE", "ASSET", "LIABILITY", "EQUITY")
        if money_out else ("INCOME", "OTHER_INCOME", "ASSET", "LIABILITY", "EQUITY")
    )
    for t in types:
        r = await client.get("/api/v1/accounts", params={"account_type": t, "limit": 500, "offset": 0})
        if r.is_success:
            gl.extend(a for a in r.json().get("items", []) if not a.get("is_header"))
    gl.sort(key=lambda a: a.get("code", ""))
    tax_codes: list[dict] = []
    r = await client.get("/api/v1/tax_codes", params={"page_size": 500})
    if r.is_success:
        tax_codes = r.json().get("items", [])
    return {"contacts": contacts, "gl_accounts": gl, "tax_codes": tax_codes}


async def _open_documents(client: Any, contact_id: str, *, money_out: bool) -> list[dict]:
    """Posted bills (money out) or invoices (money in) still owing for a contact."""
    path = "/api/v1/bills" if money_out else "/api/v1/invoices"
    docs: list[dict] = []
    r = await client.get(path, params={"contact_id": contact_id, "page_size": 200})
    if r.is_success:
        for d in r.json().get("items", []):
            status = str(d.get("status", "")).upper()
            if status not in ("POSTED", "PARTIALLY_PAID", "OVERDUE", "SENT", "AUTHORISED"):
                continue
            owing = (_dec(d.get("total")) or Decimal("0")) - (_dec(d.get("amount_paid")) or Decimal("0"))
            if owing > 0:
                d["owing"] = str(owing)
                docs.append(d)
    docs.sort(key=lambda d: str(d.get("due_date") or ""))
    return docs


@router.get(
    "/reconciliation/{bsl_id}/create",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_create_form(
    request: Request,
    bsl_id: str,
    account_id: str = "",
    kind: str = "",
    contact_id: str = "",
) -> HTMLResponse | RedirectResponse:
    """Create a record straight from a bank line (P0-16)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    line: dict | None = None
    options: dict[str, list[dict]] = {"contacts": [], "gl_accounts": [], "tax_codes": []}
    docs: list[dict] = []
    accounts: list[dict] = []
    counterpart: dict | None = None
    remaining = Decimal("0")
    degraded = False
    error: str | None = None
    try:
        async with api_client(request) as client:
            line = await _line(client, bsl_id)
            if line is None:
                error = _("That bank line was not found.")
            else:
                money_out = Decimal(str(line["amount"])) < 0
                kinds = _KINDS_OUT if money_out else _KINDS_IN
                kind = kind if kind in kinds else kinds[0]
                remaining = abs(await _remaining_on_line(client, line))
                options = await _form_options(client, money_out=money_out)
                accounts = [a for a in await _accounts(client) if a.get("id") != line["account_id"]]
                if kind in ("bill_payment", "customer_payment") and contact_id:
                    docs = await _open_documents(client, contact_id, money_out=money_out)
                other = request.query_params.get("other_account_id", "")
                if kind == "transfer" and other:
                    cp = await client.get(
                        f"/api/v1/reconciliation/transfer_counterpart/{bsl_id}",
                        params={"other_account_id": other},
                    )
                    if cp.is_success:
                        counterpart = cp.json().get("counterpart")
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/create.html",
        {
            "bsl_id": bsl_id,
            "account_id": account_id or (line or {}).get("account_id", ""),
            "line": line,
            "kind": kind,
            "contact_id": contact_id,
            "other_account_id": request.query_params.get("other_account_id", ""),
            "counterpart": counterpart,
            "docs": docs,
            "accounts": accounts,
            "remaining": remaining,
            "error": error,
            "degraded": degraded,
            "flash": request.session.pop("flash", None),
            **options,
        },
    )


@router.post("/reconciliation/{bsl_id}/create", response_model=None)
async def reconciliation_create(request: Request, bsl_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    account_id = str(form.get("account_id", "")).strip()
    kind = str(form.get("kind", "")).strip()
    contact_id = str(form.get("contact_id", "")).strip() or None
    back = f"/reconciliation/{bsl_id}/create?account_id={account_id}&kind={kind}"
    if contact_id:
        back += f"&contact_id={contact_id}"
    amount = _dec(form.get("amount"))
    payload: dict[str, Any] = {"bsl_id": bsl_id}
    if amount is not None:
        payload["amount"] = str(abs(amount))
    simple = {
        "account_id": str(form.get("gl_account_id", "")).strip() or None,
        "tax_code_id": str(form.get("tax_code_id", "")).strip() or None,
        "description": str(form.get("description", "")).strip() or None,
        "reference": str(form.get("reference", "")).strip() or None,
        "contact_id": contact_id,
    }
    if kind == "expense":
        if not simple["account_id"]:
            request.session["flash"] = _("Choose the account this spending goes to.")
            return RedirectResponse(url=back, status_code=303)
        payload.update({"record_type": "expense", "expense": simple})
    elif kind == "receipt":
        if not simple["account_id"]:
            request.session["flash"] = _("Choose the account this money goes to.")
            return RedirectResponse(url=back, status_code=303)
        payload.update({"record_type": "receipt", "receipt": simple})
    elif kind in ("bill_payment", "customer_payment"):
        if not contact_id:
            request.session["flash"] = _("Choose who the payment is with.")
            return RedirectResponse(url=back, status_code=303)
        allocations = []
        doc_key = "bill_id" if kind == "bill_payment" else "invoice_id"
        for key, value in form.multi_items():
            if key.startswith("alloc_"):
                amt = _dec(value)
                if amt is not None and amt > 0:
                    allocations.append({doc_key: key[len("alloc_"):], "amount": str(amt)})
        payload.update(
            {
                "record_type": "payment",
                "payment": {
                    "contact_id": contact_id,
                    "reference": simple["reference"],
                    "allocations": allocations,
                },
            }
        )
    elif kind == "transfer":
        other = str(form.get("other_account_id", "")).strip()
        if not other:
            request.session["flash"] = _("Choose the other account for this transfer.")
            return RedirectResponse(url=back, status_code=303)
        spec: dict[str, Any] = {
            "other_account_id": other,
            "description": simple["description"],
            "reference": simple["reference"],
        }
        cp = str(form.get("counterpart_line_id", "")).strip()
        if cp:
            spec["counterpart_line_id"] = cp
        payload.update({"record_type": "transfer", "transfer": spec})
    else:
        request.session["flash"] = _("Choose what this bank line is.")
        return RedirectResponse(url=back, status_code=303)

    try:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/reconciliation/create_and_match", json=payload)
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=back, status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        body = resp.json()
        status = (body.get("bsl") or {}).get("status")
        msg = _("Recorded and matched.") if status == "MATCHED" else _(
            "Recorded. Part of the line is still to match."
        )
        if body.get("counterpart_bsl_id"):
            msg += " " + _("The other account's bank line was matched too.")
        request.session["flash"] = msg
        return RedirectResponse(url=_lines_url(account_id), status_code=303)
    request.session["flash"] = _detail(resp, _("That did not work (HTTP %(code)s).") % {"code": resp.status_code})
    return RedirectResponse(url=back, status_code=303)


# ---------------------------------------------------------------------------
# Split — GET/POST /reconciliation/{bsl_id}/split
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{bsl_id}/split",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_split_form(
    request: Request, bsl_id: str, account_id: str = ""
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    line: dict | None = None
    options: dict[str, list[dict]] = {"contacts": [], "gl_accounts": [], "tax_codes": []}
    remaining = Decimal("0")
    degraded = False
    try:
        async with api_client(request) as client:
            line = await _line(client, bsl_id)
            if line is not None:
                remaining = abs(await _remaining_on_line(client, line))
                options = await _form_options(client, money_out=Decimal(str(line["amount"])) < 0)
    except ModuleUnavailable:
        degraded = True
    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/split.html",
        {
            "bsl_id": bsl_id,
            "account_id": account_id or (line or {}).get("account_id", ""),
            "line": line,
            "remaining": remaining,
            "rows": range(_SPLIT_ROWS),
            "degraded": degraded,
            "flash": request.session.pop("flash", None),
            **options,
        },
    )


@router.post("/reconciliation/{bsl_id}/split", response_model=None)
async def reconciliation_split(request: Request, bsl_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    account_id = str(form.get("account_id", "")).strip()
    money_out = str(form.get("direction", "out")) == "out"
    back = f"/reconciliation/{bsl_id}/split?account_id={account_id}"
    parts = []
    for i in range(_SPLIT_ROWS):
        amt = _dec(form.get(f"amount_{i}"))
        gl = str(form.get(f"gl_account_id_{i}", "")).strip()
        if amt is None and not gl:
            continue
        if amt is None or amt <= 0 or not gl:
            request.session["flash"] = _("Each split line needs an account and an amount.")
            return RedirectResponse(url=back, status_code=303)
        spec = {
            "account_id": gl,
            "tax_code_id": str(form.get(f"tax_code_id_{i}", "")).strip() or None,
            "description": str(form.get(f"description_{i}", "")).strip() or None,
            "contact_id": str(form.get(f"contact_id_{i}", "")).strip() or None,
        }
        rt = "expense" if money_out else "receipt"
        parts.append({"record_type": rt, "amount": str(amt), rt: spec})
    if not parts:
        request.session["flash"] = _("Add at least one split line.")
        return RedirectResponse(url=back, status_code=303)
    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/reconciliation/split_create", json={"bsl_id": bsl_id, "parts": parts}
            )
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=back, status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        request.session["flash"] = _("Line split and matched.")
        return RedirectResponse(url=_lines_url(account_id), status_code=303)
    request.session["flash"] = _detail(resp, _("That did not work (HTTP %(code)s).") % {"code": resp.status_code})
    return RedirectResponse(url=back, status_code=303)


# ---------------------------------------------------------------------------
# Ignore / put back / accept rule
# ---------------------------------------------------------------------------


async def _line_action(
    request: Request, bsl_id: str, path: str, body: dict | None, ok: str, tab: str
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    account_id = str(form.get("account_id", "")).strip()
    back = _lines_url(account_id, tab)
    try:
        async with api_client(request) as client:
            resp = await client.post(path, json=body) if body is not None else await client.post(path)
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=back, status_code=303)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    request.session["flash"] = ok if resp.is_success else _detail(
        resp, _("That did not work (HTTP %(code)s).") % {"code": resp.status_code}
    )
    return RedirectResponse(url=back, status_code=303)


@router.post("/reconciliation/{bsl_id}/ignore", response_model=None)
async def reconciliation_ignore(request: Request, bsl_id: str) -> RedirectResponse:
    form = await request.form()
    reason = str(form.get("reason", "")).strip() or None
    return await _line_action(
        request, bsl_id, f"/api/v1/reconciliation/ignore/{bsl_id}", {"reason": reason},
        _("Line set aside. It is on the Ignored tab."), "to_match",
    )


@router.post("/reconciliation/{bsl_id}/unignore", response_model=None)
async def reconciliation_unignore(request: Request, bsl_id: str) -> RedirectResponse:
    return await _line_action(
        request, bsl_id, f"/api/v1/reconciliation/unignore/{bsl_id}", None,
        _("Line put back to match."), "ignored",
    )


@router.post("/reconciliation/{bsl_id}/accept-rule", response_model=None)
async def reconciliation_accept_rule(request: Request, bsl_id: str) -> RedirectResponse:
    return await _line_action(
        request, bsl_id, f"/api/v1/reconciliation/accept_rule/{bsl_id}", None,
        _("Bank rule used. The line is matched."), "to_match",
    )


# ---------------------------------------------------------------------------
# Auto-match — POST /reconciliation/{account_id}/auto-match
# ---------------------------------------------------------------------------


@router.post(
    "/reconciliation/{account_id}/auto-match",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_auto_match(
    request: Request,
    account_id: str,
) -> RedirectResponse:
    """Auto-match every unmatched line that has exactly one sure match."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    redirect_url = f"/reconciliation/{account_id}/lines"

    try:
        async with api_client(request) as client:
            resp = await client.post(
                "/api/v1/reconciliation/auto_match",
                params={"account_id": account_id},
            )
    except ModuleUnavailable:
        request.session["flash"] = _engine_down()
        return RedirectResponse(url=redirect_url, status_code=303)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        try:
            data = resp.json()
            matched = data.get("matched", "")
            if matched != "":
                request.session["flash"] = _(
                    "Auto-match complete — %(n)s line(s) matched."
                ) % {"n": matched}
            else:
                request.session["flash"] = _("Auto-match complete.")
        except Exception:
            request.session["flash"] = _("Auto-match complete.")
    else:
        request.session["flash"] = _("Auto-match failed (HTTP %(code)s).") % {
            "code": resp.status_code
        }

    return RedirectResponse(url=redirect_url, status_code=303)
