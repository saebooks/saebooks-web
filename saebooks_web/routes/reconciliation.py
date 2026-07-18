"""Bank reconciliation views — Lane D cycle 49.

Route map
---------
GET  /reconciliation                          — accounts picker page
GET  /reconciliation/{account_id}/lines       — unmatched BSLs for one account
GET  /reconciliation/{bsl_id}/suggest         — suggested journal entries for a BSL
POST /reconciliation/match                    — match BSL to a journal entry
POST /reconciliation/{bsl_id}/unmatch         — clear a match
POST /reconciliation/{account_id}/auto-match  — auto-match all unmatched for account

Route ordering: /match MUST appear before /{bsl_id} paths.

API endpoints consumed (B/42):
- GET  /api/v1/reconciliation/accounts
- GET  /api/v1/reconciliation/unmatched?account_id=X
- GET  /api/v1/reconciliation/suggest/{bsl_id}
- POST /api/v1/reconciliation/match           body: {bsl_id, entry_id}
- POST /api/v1/reconciliation/unmatch/{bsl_id}
- POST /api/v1/reconciliation/auto_match?account_id=X → {"matched": N}

Auth guard: redirect to /login (303) if no session token.

Degrade (M2): GET routes catch ModuleUnavailable and render their page
shell with the shared degraded panel inline; POST routes flash an
engine-unreachable message and redirect back — no state was changed.

Pagination on the lines page is web-side (slice of the full unmatched
list): the engine's /api/v1/reconciliation/unmatched does not take
page params today (SPEC-NEEDED, flagged to engine lane).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_LINES_PAGE_SIZE = 50


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _api_error(status_code: int) -> str:
    return _("The reconciliation data could not be loaded (HTTP %(code)s).") % {
        "code": status_code
    }


# ---------------------------------------------------------------------------
# Accounts picker — GET /reconciliation
# ---------------------------------------------------------------------------


@router.get("/reconciliation", response_class=HTMLResponse, response_model=None)
async def reconciliation_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the accounts picker — lists reconcilable bank/cash accounts."""
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
        {
            "accounts": accounts,
            "error": error,
            "degraded": degraded,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Unmatched BSL list — GET /reconciliation/{account_id}/lines
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{account_id}/lines",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_lines(
    request: Request,
    account_id: str,
    offset: int = 0,
    limit: int = _LINES_PAGE_SIZE,
) -> HTMLResponse | RedirectResponse:
    """Render unmatched BSLs for a single reconcilable account."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    lines: list[dict] = []
    account: dict | None = None
    degraded = False

    try:
        async with api_client(request) as client:
            # Fetch the account list to get the account name for display
            acct_resp = await client.get("/api/v1/reconciliation/accounts")
            if acct_resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if acct_resp.is_success:
                all_accounts = acct_resp.json()
                if isinstance(all_accounts, list):
                    account = next((a for a in all_accounts if a.get("id") == account_id), None)

            # Fetch unmatched lines for this account
            resp = await client.get(
                "/api/v1/reconciliation/unmatched",
                params={"account_id": account_id},
            )

            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)

            if resp.is_success:
                payload = resp.json()
                lines = payload if isinstance(payload, list) else payload.get("items", [])
            else:
                error = _api_error(resp.status_code)
    except ModuleUnavailable:
        degraded = True

    flash = request.session.pop("flash", None)

    # Web-side pagination — the engine endpoint returns the full unmatched
    # list (no page params yet; SPEC-NEEDED, see module docstring).
    total = len(lines)
    offset = max(offset, 0)
    limit = limit if 0 < limit <= 200 else _LINES_PAGE_SIZE
    page_lines = lines[offset : offset + limit]

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/lines.html",
        {
            "account_id": account_id,
            "account": account,
            "lines": page_lines,
            "all_lines_total": total,
            "offset": offset,
            "limit": limit,
            "error": error,
            "degraded": degraded,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Match — POST /reconciliation/match
# NOTE: MUST appear before /{bsl_id} paths.
# ---------------------------------------------------------------------------


@router.post("/reconciliation/match", response_class=HTMLResponse, response_model=None)
async def reconciliation_match(request: Request) -> RedirectResponse:
    """Match a BSL to a journal entry.

    Reads ``bsl_id``, ``entry_id``, ``account_id`` from form body.
    POSTs ``{bsl_id, entry_id}`` to ``POST /api/v1/reconciliation/match``.
    Redirects back to the account lines page on success/failure.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    bsl_id = str(form_data.get("bsl_id", "")).strip()
    entry_id = str(form_data.get("entry_id", "")).strip()
    account_id = str(form_data.get("account_id", "")).strip()

    payload = {"bsl_id": bsl_id, "entry_id": entry_id}
    redirect_url = f"/reconciliation/{account_id}/lines" if account_id else "/reconciliation"

    try:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/reconciliation/match", json=payload)
    except ModuleUnavailable:
        request.session["flash"] = _(
            "The accounting engine could not be reached — nothing was changed. Try again in a moment."
        )
        return RedirectResponse(url=redirect_url, status_code=303)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = _("Line matched.")
    else:
        fallback = _("Match failed (HTTP %(code)s).") % {"code": resp.status_code}
        try:
            detail = resp.json().get("detail", fallback)
        except Exception:
            detail = fallback
        request.session["flash"] = str(detail)

    return RedirectResponse(url=redirect_url, status_code=303)


# ---------------------------------------------------------------------------
# Unmatch — POST /reconciliation/{bsl_id}/unmatch
# NOTE: MUST appear before the suggest GET.
# ---------------------------------------------------------------------------


@router.post(
    "/reconciliation/{bsl_id}/unmatch",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_unmatch(
    request: Request,
    bsl_id: str,
) -> RedirectResponse:
    """Remove the match from a BSL.

    POSTs to ``POST /api/v1/reconciliation/unmatch/{bsl_id}`` (no body).
    Reads ``account_id`` from form body for the redirect target.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    account_id = str(form_data.get("account_id", "")).strip()
    redirect_url = f"/reconciliation/{account_id}/lines" if account_id else "/reconciliation"

    try:
        async with api_client(request) as client:
            resp = await client.post(f"/api/v1/reconciliation/unmatch/{bsl_id}")
    except ModuleUnavailable:
        request.session["flash"] = _(
            "The accounting engine could not be reached — nothing was changed. Try again in a moment."
        )
        return RedirectResponse(url=redirect_url, status_code=303)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = _("Line unmatched.")
    else:
        fallback = _("Unmatch failed (HTTP %(code)s).") % {"code": resp.status_code}
        try:
            detail = resp.json().get("detail", fallback)
        except Exception:
            detail = fallback
        request.session["flash"] = str(detail)

    return RedirectResponse(url=redirect_url, status_code=303)


# ---------------------------------------------------------------------------
# Suggest — GET /reconciliation/{bsl_id}/suggest
# NOTE: MUST appear after /match.
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
    """Render suggested matching journal entries for a single BSL."""
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
    """Trigger auto-match for all unmatched BSLs in a given account.

    POSTs to ``POST /api/v1/reconciliation/auto_match?account_id=X``.
    Returns ``{"matched": N}``.
    """
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
        request.session["flash"] = _(
            "The accounting engine could not be reached — nothing was changed. Try again in a moment."
        )
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
