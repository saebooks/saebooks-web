"""Taxable payments annual report (TPAR) screens — P1-74, batch 14.

GET  /tpar                     — runs for the active company + "Prepare a report"
POST /tpar                     — generate (or regenerate the draft) for a financial year
GET  /tpar/{id}                — one run: totals and one row per contractor
GET  /tpar/{id}/download.csv   — the rows as a spreadsheet
GET  /tpar/{id}/download.bde   — the ATO electronic file (upload through Online services for business)
POST /tpar/{id}/finalise       — lock the draft

The engine side (``/api/v1/tpar``) is complete; this module is the missing
screen. Which suppliers are included is set per contact ("Report payments to
this contractor on the taxable payments annual report" on the contact form).
Nothing is lodged from here: the app prepares the file, the business uploads
it to the ATO.

Australian companies only — other jurisdictions get a plain "not available"
page rather than a 404.
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
    return request.session.get("api_token")


def _is_au(request: Request) -> bool:
    return (getattr(request.state, "active_company_jurisdiction", None) or "AU") == "AU"


def _error_detail(resp) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        return f"Something went wrong (HTTP {resp.status_code})."
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        return str(detail[0].get("msg", detail[0]))
    return f"Something went wrong (HTTP {resp.status_code})."


def _fy_options(today: date | None = None) -> list[dict[str, str]]:
    """The last four Australian financial years, newest first."""
    today = today or date.today()
    start_year = today.year if today.month >= 7 else today.year - 1
    out = []
    for y in range(start_year, start_year - 4, -1):
        out.append({
            "fy_start": date(y, 7, 1).isoformat(),
            "label": f"1 July {y} to 30 June {y + 1}",
        })
    return out


def _default_fy(today: date | None = None) -> str:
    """The year a TPAR is normally due for: the one that ended last 30 June."""
    today = today or date.today()
    start_year = today.year if today.month >= 7 else today.year - 1
    return date(start_year - 1, 7, 1).isoformat()


@router.get("/tpar", response_class=HTMLResponse, response_model=None)
async def tpar_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_au(request):
        return _TEMPLATES.TemplateResponse(
            request, "tpar/list.html",
            {"runs": [], "error": None, "flash": None, "not_au": True,
             "fy_options": [], "default_fy": "", "contractor_count": None},
        )

    error = None
    runs: list[dict] = []
    contractor_count: int | None = None
    async with api_client(request) as client:
        resp = await client.get("/api/v1/tpar")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            runs = resp.json().get("items", [])
        else:
            error = _error_detail(resp)
        c_resp = await client.get(
            "/api/v1/contacts", params={"limit": 500, "offset": 0}
        )
        if c_resp.is_success:
            contractor_count = sum(
                1 for c in c_resp.json().get("items", []) if c.get("is_tpar_supplier")
            )

    return _TEMPLATES.TemplateResponse(
        request,
        "tpar/list.html",
        {
            "runs": runs,
            "error": error,
            "flash": request.session.pop("flash", None),
            "not_au": False,
            "fy_options": _fy_options(),
            "default_fy": _default_fy(),
            "contractor_count": contractor_count,
        },
    )


@router.post("/tpar", response_class=HTMLResponse, response_model=None)
async def tpar_generate(request: Request) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    fy_start = str(form.get("fy_start", "")).strip()
    payload: dict[str, str] = {}
    if fy_start:
        payload["fy_start"] = fy_start
    async with api_client(request) as client:
        resp = await client.post("/api/v1/tpar", json=payload)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 201:
        run_id = resp.json()["id"]
        request.session["flash"] = "Report prepared. Check each contractor before you finalise it."
        return RedirectResponse(url=f"/tpar/{run_id}", status_code=303)
    request.session["flash"] = _error_detail(resp)
    return RedirectResponse(url="/tpar", status_code=303)


@router.get("/tpar/{run_id}/download.csv", response_model=None)
async def tpar_csv(request: Request, run_id: str) -> Response:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/tpar/{run_id}/lines.csv")
    if not resp.is_success:
        request.session["flash"] = _error_detail(resp)
        return RedirectResponse(url=f"/tpar/{run_id}", status_code=303)
    return Response(
        content=resp.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="tpar-{run_id}.csv"'},
    )


@router.get("/tpar/{run_id}/download.bde", response_model=None)
async def tpar_bde(request: Request, run_id: str) -> Response:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/tpar/{run_id}/lines.bde")
    if not resp.is_success:
        request.session["flash"] = _error_detail(resp)
        return RedirectResponse(url=f"/tpar/{run_id}", status_code=303)
    return Response(
        content=resp.content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="tpar-{run_id}.bde.txt"'},
    )


@router.post("/tpar/{run_id}/finalise", response_model=None)
async def tpar_finalise(request: Request, run_id: str) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/tpar/{run_id}/finalise", json={})
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    request.session["flash"] = (
        "Report finalised. Download the ATO file and upload it through Online services for business."
        if resp.is_success else _error_detail(resp)
    )
    return RedirectResponse(url=f"/tpar/{run_id}", status_code=303)


@router.get("/tpar/{run_id}", response_class=HTMLResponse, response_model=None)
async def tpar_detail(request: Request, run_id: str) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/tpar/{run_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return _TEMPLATES.TemplateResponse(
                request, "tpar/detail.html",
                {"run": None, "lines": [], "error": "Report not found.", "flash": None},
                status_code=404,
            )
        run = resp.json() if resp.is_success else None
        lines: list[dict] = []
        if run is not None:
            l_resp = await client.get(f"/api/v1/tpar/{run_id}/lines")
            if l_resp.is_success:
                lines = l_resp.json().get("items", [])
    missing_abn = sum(1 for ln in lines if not (ln.get("payee_abn") or "").strip())
    return _TEMPLATES.TemplateResponse(
        request,
        "tpar/detail.html",
        {
            "run": run,
            "lines": lines,
            "missing_abn": missing_abn,
            "error": None if run else _error_detail(resp),
            "flash": request.session.pop("flash", None),
        },
    )
