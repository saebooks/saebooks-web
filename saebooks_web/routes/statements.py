"""Supplier statement reconciliation views — Gitea #28, Phase 1.

Route map
---------
GET  /statements              — recon queue: table of statements + ingest form
GET  /statements/{id}         — recon detail: header card + lines table
POST /statements/ingest       — ingest a Paperless doc → redirect to detail

Route ordering: /statements/ingest MUST be declared before /statements/{id}
so FastAPI resolves the literal path first.

API endpoints consumed:
- GET  /api/v1/statements?status=&limit=&offset=
- GET  /api/v1/statements/{id}
- POST /api/v1/statements/ingest  body: {"paperless_document_id": int}

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


# Match-status sort order: exceptions first, then matched, then informational.
# Lower rank = listed earlier.
_MATCH_STATUS_RANK: dict[str, int] = {
    "missing_in_books":     0,
    "amount_mismatch":      1,
    "not_on_statement":     2,
    "matched":              3,
    "settled_not_in_books": 4,
    "payment_info":         5,
}


def _sort_lines(lines: list[dict]) -> list[dict]:
    """Sort statement lines: exceptions first, then matched, then informational."""
    return sorted(
        lines,
        key=lambda ln: _MATCH_STATUS_RANK.get(
            (ln.get("match_status") or "").lower(), 99
        ),
    )


# ---------------------------------------------------------------------------
# GET /statements — recon queue
# ---------------------------------------------------------------------------


@router.get("/statements", response_class=HTMLResponse, response_model=None)
async def statements_list(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the supplier-statement reconciliation queue."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    statements: list[dict] = []
    total: int = 0

    params: dict[str, str | int] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status

    async with api_client(request) as client:
        resp = await client.get("/api/v1/statements", params=params)

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.is_success:
            payload = resp.json()
            statements = payload.get("items", [])
            total = payload.get("total", len(statements))
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "statements/index.html",
        {
            "statements": statements,
            "total": total,
            "status_filter": status or "",
            "limit": limit,
            "offset": offset,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# POST /statements/ingest — ingest a Paperless document
# NOTE: MUST appear before /statements/{id}
# ---------------------------------------------------------------------------


@router.post("/statements/ingest", response_class=HTMLResponse, response_model=None)
async def statements_ingest(request: Request) -> RedirectResponse:
    """Ingest a Paperless document by ID → redirect to the new statement detail.

    On error, redirects back to /statements with a flash message.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    raw = str(form_data.get("paperless_document_id", "")).strip()

    if not raw.isdigit():
        request.session["flash"] = "Invalid document ID — must be a number."
        return RedirectResponse(url="/statements", status_code=303)

    doc_id = int(raw)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/statements/ingest",
            json={"paperless_document_id": doc_id},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (200, 201):
        try:
            detail = resp.json()
            stmt_id = detail.get("id")
            if stmt_id:
                return RedirectResponse(url=f"/statements/{stmt_id}", status_code=303)
        except Exception:  # noqa: BLE001
            pass
        request.session["flash"] = "Statement ingested."
        return RedirectResponse(url="/statements", status_code=303)

    # Error path — put detail in flash and return to queue
    try:
        msg = resp.json().get("detail", f"Ingest failed: HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001
        msg = f"Ingest failed: HTTP {resp.status_code}"
    request.session["flash"] = str(msg)
    return RedirectResponse(url="/statements", status_code=303)


# ---------------------------------------------------------------------------
# GET /statements/{id} — recon detail
# NOTE: MUST appear after /statements/ingest
# ---------------------------------------------------------------------------


@router.get("/statements/{statement_id}", response_class=HTMLResponse, response_model=None)
async def statements_detail(
    request: Request,
    statement_id: str,
) -> HTMLResponse | RedirectResponse:
    """Render the reconciliation detail for a single supplier statement."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    error: str | None = None
    statement: dict | None = None
    lines: list[dict] = []

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/statements/{statement_id}")

        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if resp.status_code == 404:
            return HTMLResponse(
                _TEMPLATES.get_template("statements/detail.html").render(
                    {
                        "request": request,
                        "statement": None,
                        "lines": [],
                        "error": "Statement not found.",
                        "flash": None,
                    }
                ),
                status_code=404,
            )

        if resp.is_success:
            statement = resp.json()
            lines = _sort_lines(statement.pop("lines", []))
        else:
            error = f"API error: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "statements/detail.html",
        {
            "statement": statement,
            "lines": lines,
            "error": error,
            "flash": flash,
        },
    )
