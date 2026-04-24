"""Bank statement lines list, detail, and reconciliation views — Lane D cycles 27 + 36.

GET  /bank-statement-lines                  — list page (paginated, HTMX-aware)
POST /bank-statement-lines/{id}/match       — match a line to a payment or JE
POST /bank-statement-lines/{id}/unmatch     — remove a match from a line
GET  /bank-statement-lines/{id}             — bank statement line detail

Route ordering: /match and /unmatch MUST appear before the catch-all /{line_id} GET
so FastAPI resolves the literal paths first.

Auth guard: redirect to /login (303) if no session token.

The API uses limit/offset pagination and the prefix is /api/v1/bank_statement_lines.
Filters: bank_account_id (UUID), status (UNMATCHED/MATCHED/IGNORED/RECONCILED).
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


@router.get("/bank-statement-lines", response_class=HTMLResponse, response_model=None)
async def bank_statement_lines_list(
    request: Request,
    status: str | None = None,
    bank_account_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the bank statement lines list page (full or HTMX fragment).

    When the request carries an ``HX-Request: true`` header the response is
    the ``bank_statement_lines/_table.html`` partial only.  Otherwise the full
    page (``bank_statement_lines/list.html``) is returned.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    params: dict[str, object] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if bank_account_id:
        params["bank_account_id"] = bank_account_id

    error: str | None = None
    lines: list[dict] = []
    total: int = 0

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank_statement_lines", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            lines = payload.get("items", [])
            total = payload.get("total", len(lines))
        else:
            error = f"API error: HTTP {resp.status_code}"

    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    flash = request.session.pop("flash", None)

    ctx = {
        "lines": lines,
        "total": total,
        "error": error,
        "flash": flash,
        "filter_status": status or "",
        "filter_bank_account_id": bank_account_id or "",
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = (
        "bank_statement_lines/_table.html" if is_htmx
        else "bank_statement_lines/list.html"
    )

    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Match — POST /bank-statement-lines/{line_id}/match
# NOTE: MUST appear before the catch-all /{line_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/bank-statement-lines/{line_id}/match",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_statement_line_match(
    request: Request,
    line_id: str,
) -> HTMLResponse | RedirectResponse:
    """Match a bank statement line to a payment or journal entry.

    Reads ``matched_to_type`` and ``matched_to_id`` from the form body and
    POSTs to ``POST /api/v1/bank_statement_lines/{id}/match``.

    - 200 -> 303 redirect to detail with flash "Line matched."
    - 422 -> re-render detail with the API's error message as flash
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    matched_to_type = str(form_data.get("matched_to_type", "")).strip()
    matched_to_id = str(form_data.get("matched_to_id", "")).strip()

    payload: dict[str, object] = {
        "matched_to_type": matched_to_type,
        "matched_to_id": matched_to_id,
    }

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/bank_statement_lines/{line_id}/match",
            json=payload,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Line matched."
        return RedirectResponse(
            url=f"/bank-statement-lines/{line_id}", status_code=303
        )

    # 422 or other error — surface the API's detail message as flash, then
    # re-fetch and re-render the detail page so the user can correct the input.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    error_msg = str(detail)

    async with api_client(request) as client:
        line_resp = await client.get(f"/api/v1/bank_statement_lines/{line_id}")

    if not line_resp.is_success:
        return _TEMPLATES.TemplateResponse(
            request,
            "bank_statement_lines/detail.html",
            {"line": None, "error": error_msg, "flash": None},
            status_code=422,
        )

    line = line_resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "bank_statement_lines/detail.html",
        {
            "line": line,
            "error": error_msg,
            "flash": None,
            "match_form": {
                "matched_to_type": matched_to_type,
                "matched_to_id": matched_to_id,
            },
        },
        status_code=422,
    )


# ---------------------------------------------------------------------------
# Unmatch — POST /bank-statement-lines/{line_id}/unmatch
# NOTE: MUST appear before the catch-all /{line_id} GET.
# ---------------------------------------------------------------------------


@router.post(
    "/bank-statement-lines/{line_id}/unmatch",
    response_class=HTMLResponse,
    response_model=None,
)
async def bank_statement_line_unmatch(
    request: Request,
    line_id: str,
) -> HTMLResponse | RedirectResponse:
    """Remove the match from a bank statement line.

    POSTs to ``POST /api/v1/bank_statement_lines/{id}/unmatch`` (no body).

    - 200 -> 303 redirect to detail with flash "Line unmatched."
    - 422 -> 303 back to detail with the API's error message as flash
    - 401 -> clear session, redirect to /login
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/bank_statement_lines/{line_id}/unmatch",
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        request.session["flash"] = "Line unmatched."
        return RedirectResponse(
            url=f"/bank-statement-lines/{line_id}", status_code=303
        )

    # 422 or other — surface the API's detail message as flash and redirect back.
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")
        if isinstance(detail, list) and detail:
            detail = detail[0].get("msg", str(detail))
    except Exception:
        detail = f"API error: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url=f"/bank-statement-lines/{line_id}", status_code=303)


@router.get("/bank-statement-lines/{line_id}", response_class=HTMLResponse, response_model=None)
async def bank_statement_line_detail(
    request: Request,
    line_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render a single bank statement line detail page."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/bank_statement_lines/{line_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_statement_lines/detail.html",
                {"line": None, "error": "Bank statement line not found", "flash": None},
                status_code=404,
            )
        if not resp.is_success:
            return _TEMPLATES.TemplateResponse(
                request,
                "bank_statement_lines/detail.html",
                {"line": None, "error": f"API error: HTTP {resp.status_code}", "flash": None},
                status_code=resp.status_code,
            )

    line = resp.json()
    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "bank_statement_lines/detail.html",
        {"line": line, "error": None, "flash": flash},
    )
