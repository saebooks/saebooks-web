"""Bank reconciliation views — Lane D cycle 48.

GET  /reconciliation                    — main page: unmatched BSLs
GET  /reconciliation/{bsl_id}/suggest   — suggested matches for a BSL
POST /reconciliation/match              — match a BSL to a transaction
POST /reconciliation/{bsl_id}/unmatch   — remove a match from a BSL
POST /reconciliation/auto-match         — trigger auto-match for all BSLs

Route ordering: /match + /auto-match MUST appear before /{bsl_id} paths so
FastAPI matches literal paths first.

API calls:
- GET  /api/v1/reconciliation/unmatched
- GET  /api/v1/reconciliation/suggest/{bsl_id}
- POST /api/v1/reconciliation/match
- POST /api/v1/reconciliation/unmatch/{bsl_id}
- POST /api/v1/reconciliation/auto_match

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
# Main reconciliation page — unmatched BSLs
# ---------------------------------------------------------------------------


@router.get("/reconciliation", response_class=HTMLResponse, response_model=None)
async def reconciliation_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the main reconciliation page with unmatched bank statement lines."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    lines: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get("/api/v1/reconciliation/unmatched")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            if isinstance(payload, list):
                lines = payload
            else:
                lines = payload.get("items", [])
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/index.html",
        {
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
    """Match a BSL to a transaction.

    Reads ``bsl_id``, ``transaction_type``, ``transaction_id`` from form body
    and POSTs to ``POST /api/v1/reconciliation/match``.

    - 200 -> 303 redirect to /reconciliation
    - 401 -> clear session, redirect to /login
    - other -> redirect with error flash
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    payload = {
        "bsl_id": str(form_data.get("bsl_id", "")).strip(),
        "transaction_type": str(form_data.get("transaction_type", "")).strip(),
        "transaction_id": str(form_data.get("transaction_id", "")).strip(),
    }

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

    return RedirectResponse(url="/reconciliation", status_code=303)


# ---------------------------------------------------------------------------
# Auto-match — POST /reconciliation/auto-match
# NOTE: MUST appear before /{bsl_id} paths.
# ---------------------------------------------------------------------------


@router.post("/reconciliation/auto-match", response_class=HTMLResponse, response_model=None)
async def reconciliation_auto_match(request: Request) -> RedirectResponse:
    """Trigger auto-match for all unmatched BSLs.

    POSTs to ``POST /api/v1/reconciliation/auto_match`` and redirects to
    /reconciliation with a flash message showing how many lines were matched.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post("/api/v1/reconciliation/auto_match")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        try:
            data = resp.json()
            matched = data.get("matched", data.get("count", ""))
            if matched != "":
                request.session["flash"] = f"Auto-match complete — {matched} line(s) matched."
            else:
                request.session["flash"] = "Auto-match complete."
        except Exception:
            request.session["flash"] = "Auto-match complete."
    else:
        request.session["flash"] = f"Auto-match failed: HTTP {resp.status_code}"

    return RedirectResponse(url="/reconciliation", status_code=303)


# ---------------------------------------------------------------------------
# Unmatch — POST /reconciliation/{bsl_id}/unmatch
# NOTE: MUST appear before the catch-all /{bsl_id}/suggest GET.
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

    - 200 -> 303 redirect to /reconciliation
    - 401 -> clear session, redirect to /login
    - other -> redirect with error flash
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

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

    return RedirectResponse(url="/reconciliation", status_code=303)


# ---------------------------------------------------------------------------
# Suggest — GET /reconciliation/{bsl_id}/suggest
# NOTE: MUST appear after /match and /auto-match.
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliation/{bsl_id}/suggest",
    response_class=HTMLResponse,
    response_model=None,
)
async def reconciliation_suggest(
    request: Request,
    bsl_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render suggested matching transactions for a single BSL."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    suggestions: list[dict] = []
    bsl: dict | None = None

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/reconciliation/suggest/{bsl_id}")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            if isinstance(payload, list):
                suggestions = payload
            else:
                suggestions = payload.get("suggestions", [])
                bsl = payload.get("bsl")
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "reconciliation/suggest.html",
        {
            "bsl_id": bsl_id,
            "bsl": bsl,
            "suggestions": suggestions,
            "error": error,
            "flash": flash,
        },
    )
