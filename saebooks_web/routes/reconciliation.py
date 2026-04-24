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

    async with api_client(request) as client:
        resp = await client.get("/api/v1/reconciliation/accounts")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            accounts = payload if isinstance(payload, list) else payload.get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/index.html",
        {
            "accounts": accounts,
            "error": error,
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
) -> HTMLResponse | RedirectResponse:
    """Render unmatched BSLs for a single reconcilable account."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    lines: list[dict] = []
    account: dict | None = None

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
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/lines.html",
        {
            "account_id": account_id,
            "account": account,
            "lines": lines,
            "error": error,
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

    async with api_client(request) as client:
        resp = await client.post("/api/v1/reconciliation/match", json=payload)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = "Line matched."
    else:
        try:
            detail = resp.json().get("detail", f"Match failed: HTTP {resp.status_code}")
        except Exception:
            detail = f"Match failed: HTTP {resp.status_code}"
        request.session["flash"] = str(detail)

    redirect_url = f"/reconciliation/{account_id}/lines" if account_id else "/reconciliation"
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

    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/reconciliation/unmatch/{bsl_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        request.session["flash"] = "Line unmatched."
    else:
        try:
            detail = resp.json().get("detail", f"Unmatch failed: HTTP {resp.status_code}")
        except Exception:
            detail = f"Unmatch failed: HTTP {resp.status_code}"
        request.session["flash"] = str(detail)

    redirect_url = f"/reconciliation/{account_id}/lines" if account_id else "/reconciliation"
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

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/reconciliation/suggest/{bsl_id}")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            suggestions = payload if isinstance(payload, list) else payload.get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/suggest.html",
        {
            "bsl_id": bsl_id,
            "account_id": account_id or "",
            "suggestions": suggestions,
            "error": error,
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

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/reconciliation/auto_match",
            params={"account_id": account_id},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        try:
            data = resp.json()
            matched = data.get("matched", "")
            if matched != "":
                request.session["flash"] = f"Auto-match complete — {matched} line(s) matched."
            else:
                request.session["flash"] = "Auto-match complete."
        except Exception:
            request.session["flash"] = "Auto-match complete."
    else:
        request.session["flash"] = f"Auto-match failed: HTTP {resp.status_code}"

    return RedirectResponse(url=f"/reconciliation/{account_id}/lines", status_code=303)
