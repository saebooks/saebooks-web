"""Imports wizard views — Lane D cycle 54.

Route map
---------
GET  /admin/imports/              — landing with three options (bank, CoA, QBO)
GET  /admin/imports/bank          — bank statement import: upload form
POST /admin/imports/bank/preview  — parse CSV/OFX, render preview table
POST /admin/imports/bank/apply    — confirm import, persist via API
GET  /admin/imports/coa           — CoA import: upload form + export link
POST /admin/imports/coa/preview   — parse CoA CSV, render diff table
POST /admin/imports/coa/apply     — confirm CoA diff, apply via API

API endpoints consumed (proxied):
- POST /admin/imports/bank/preview (multipart)    → HTML preview or error
- POST /admin/imports/bank/apply  (form)           → HTML done or error
- POST /admin/imports/coa/preview (multipart)      → HTML diff
- POST /admin/imports/coa/apply   (form)           → redirect with query string

Since the upstream API returns HTML for these routes, we proxy the form
submissions and render our own wrapper templates for nav consistency.

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


def _require_admin(request: Request) -> bool:
    """Return True if the session user has admin (or SAE staff) access.

    Tenant admins and SAE staff may both access imports — SAE staff always
    gets through; tenant users with role != 'admin' are refused.
    """
    role = request.session.get("user_role", "")
    is_staff = bool(request.session.get("is_sae_staff"))
    return is_staff or role == "admin"


# ---------------------------------------------------------------------------
# Landing — GET /admin/imports/
# ---------------------------------------------------------------------------


@router.get("/admin/imports", response_class=HTMLResponse, response_model=None)
@router.get("/admin/imports/", response_class=HTMLResponse, response_model=None)
async def imports_landing(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the imports landing page with links to bank, CoA, and QBO flows."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "imports/landing.html",
        {"flash": flash},
    )


# ---------------------------------------------------------------------------
# Bank import — GET /admin/imports/bank (upload form)
# ---------------------------------------------------------------------------


@router.get("/admin/imports/bank", response_class=HTMLResponse, response_model=None)
async def imports_bank_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the bank statement upload form with bank-account picker."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    bank_accounts: list[dict] = []
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/api/v1/bank-accounts", params={"limit": 200, "offset": 0})
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            bank_accounts = payload.get("items", payload) if isinstance(payload, dict) else payload
        else:
            error = f"API error fetching bank accounts: HTTP {resp.status_code}"

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "imports/bank.html",
        {
            "bank_accounts": bank_accounts,
            "error": error,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Bank import — POST /admin/imports/bank/preview (parse + preview)
# ---------------------------------------------------------------------------


@router.post("/admin/imports/bank/preview", response_class=HTMLResponse, response_model=None)
async def imports_bank_preview(request: Request) -> HTMLResponse | RedirectResponse:
    """Upload bank CSV/OFX, proxy to API parser, render preview.

    Forwards the multipart form to ``POST /admin/imports/bank/preview`` on the
    upstream API.  The API returns an HTML preview page; we embed the proxy
    content in our wrapper template.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()

    # Rebuild multipart for upstream — include file upload and account_id.
    account_id = str(form_data.get("account_id", ""))
    file_field = form_data.get("file")

    files: dict | None = None
    if hasattr(file_field, "read"):
        content = await file_field.read()  # type: ignore[union-attr]
        filename = getattr(file_field, "filename", "upload.csv") or "upload.csv"
        files = {"file": (filename, content, "application/octet-stream")}

    proxy_html: str | None = None
    error: str | None = None

    async with api_client(request) as client:
        if files:
            resp = await client.post(
                "/admin/imports/bank/preview",
                data={"account_id": account_id},
                files=files,
            )
        else:
            resp = await client.post(
                "/admin/imports/bank/preview",
                data={"account_id": account_id},
            )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        proxy_html = resp.text
    else:
        try:
            detail = resp.json().get("detail", f"Preview failed: HTTP {resp.status_code}")
        except Exception:
            detail = f"Preview failed: HTTP {resp.status_code}"
        error = str(detail)

    return _TEMPLATES.TemplateResponse(
        request,
        "imports/bank_preview.html",
        {
            "proxy_html": proxy_html,
            "account_id": account_id,
            "error": error,
        },
        status_code=200 if proxy_html else 400,
    )


# ---------------------------------------------------------------------------
# Bank import — POST /admin/imports/bank/apply (confirm + persist)
# ---------------------------------------------------------------------------


@router.post("/admin/imports/bank/apply", response_class=HTMLResponse, response_model=None)
async def imports_bank_apply(request: Request) -> HTMLResponse | RedirectResponse:
    """Confirm bank import — proxy to API apply endpoint."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}  # type: ignore[misc]

    async with api_client(request) as client:
        resp = await client.post("/admin/imports/bank/apply", data=form)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        proxy_html = resp.text
        return _TEMPLATES.TemplateResponse(
            request,
            "imports/bank_done.html",
            {"proxy_html": proxy_html},
        )

    try:
        detail = resp.json().get("detail", f"Import failed: HTTP {resp.status_code}")
    except Exception:
        detail = f"Import failed: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url="/admin/imports/bank", status_code=303)


# ---------------------------------------------------------------------------
# CoA import — GET /admin/imports/coa (upload form)
# ---------------------------------------------------------------------------


@router.get("/admin/imports/coa", response_class=HTMLResponse, response_model=None)
async def imports_coa_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the CoA import upload form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    flash = request.session.pop("flash", None)

    # Pass through any result params from a successful apply redirect.
    params = dict(request.query_params)
    return _TEMPLATES.TemplateResponse(
        request,
        "imports/coa.html",
        {
            "flash": flash,
            "result": params if params else None,
        },
    )


# ---------------------------------------------------------------------------
# CoA import — POST /admin/imports/coa/preview (parse + diff)
# ---------------------------------------------------------------------------


@router.post("/admin/imports/coa/preview", response_class=HTMLResponse, response_model=None)
async def imports_coa_preview(request: Request) -> HTMLResponse | RedirectResponse:
    """Upload CoA CSV, proxy to API parser, render diff preview."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    file_field = form_data.get("file")

    files: dict | None = None
    if hasattr(file_field, "read"):
        content = await file_field.read()  # type: ignore[union-attr]
        filename = getattr(file_field, "filename", "coa.csv") or "coa.csv"
        files = {"file": (filename, content, "text/csv")}

    proxy_html: str | None = None
    error: str | None = None

    async with api_client(request) as client:
        if files:
            resp = await client.post("/admin/imports/coa/preview", files=files)
        else:
            resp = await client.post("/admin/imports/coa/preview")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        proxy_html = resp.text
    else:
        try:
            detail = resp.json().get("detail", f"Preview failed: HTTP {resp.status_code}")
        except Exception:
            detail = f"Preview failed: HTTP {resp.status_code}"
        error = str(detail)

    return _TEMPLATES.TemplateResponse(
        request,
        "imports/coa_preview.html",
        {
            "proxy_html": proxy_html,
            "error": error,
        },
        status_code=200 if proxy_html else 400,
    )


# ---------------------------------------------------------------------------
# CoA import — POST /admin/imports/coa/apply (confirm + apply)
# ---------------------------------------------------------------------------


@router.post("/admin/imports/coa/apply", response_model=None)
async def imports_coa_apply(request: Request) -> RedirectResponse:
    """Confirm CoA diff — proxy to API apply endpoint and redirect."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {k: str(v) for k, v in form_data.items()}  # type: ignore[misc]

    async with api_client(request) as client:
        resp = await client.post(
            "/admin/imports/coa/apply",
            data=form,
            follow_redirects=False,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (301, 302, 303, 307, 308):
        # Follow the redirect location from the API, translated to our path.
        location = resp.headers.get("location", "/admin/imports/coa")
        # Strip the upstream host if present and map to our path.
        if location.startswith("/admin/imports/coa"):
            return RedirectResponse(url=location, status_code=303)
        return RedirectResponse(url="/admin/imports/coa?applied=1", status_code=303)

    if resp.is_success:
        request.session["flash"] = "CoA import applied."
        return RedirectResponse(url="/admin/imports/coa?applied=1", status_code=303)

    try:
        detail = resp.json().get("detail", f"Apply failed: HTTP {resp.status_code}")
    except Exception:
        detail = f"Apply failed: HTTP {resp.status_code}"
    request.session["flash"] = str(detail)
    return RedirectResponse(url="/admin/imports/coa", status_code=303)
