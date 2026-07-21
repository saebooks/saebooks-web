"""Imports wizard views — Cat-C rewrite (v1 API backend).

Route map
---------
GET  /admin/imports/              — landing with options (bank CSV/OFX, CoA, QBO)
POST /admin/imports/bank/start    — start a bank_csv or bank_ofx wizard via API
POST /admin/imports/bank/upload   — step: post the raw file content
POST /admin/imports/bank/commit   — commit the wizard
POST /admin/imports/coa/start     — start a coa wizard via API
POST /admin/imports/coa/upload    — step: post the CoA CSV content
POST /admin/imports/coa/commit    — commit the CoA wizard
POST /admin/imports/qbo/start     — start a qbo wizard (Pro+ only)
POST /admin/imports/qbo/contacts  — step: post QBO contacts CSV
POST /admin/imports/qbo/accounts  — step: post QBO accounts CSV
POST /admin/imports/qbo/commit    — commit the QBO wizard

All forms use POST-redirect-GET.  The wizard_id is carried in the session
between steps.  No raw file bytes are stored in the session — only the
wizard_id; the file content lives in the wizard_state JSONB in Postgres.

Auth guard: redirect to /login (303) if no session token.
Admin guard: role in ("owner", "admin") or is_sae_staff required for imports.

API endpoints consumed:
    POST /api/v1/imports/wizards              → start wizard
    POST /api/v1/imports/wizards/{id}/step   → advance wizard state
    GET  /api/v1/imports/wizards/{id}         → read wizard state
    POST /api/v1/imports/wizards/{id}/commit  → run import

Error handling: API errors are extracted from JSON detail or HTTP status
and shown as a flash message on the form page.
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

_WIZARD_KEY = "import_wizard_id"
_WIZARD_KIND_KEY = "import_wizard_kind"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _require_admin(request: Request) -> bool:
    role = request.session.get("user_role", "")
    is_staff = bool(request.session.get("is_sae_staff"))
    return is_staff or role in ("owner", "admin")


def _auth_redirect(request: Request) -> RedirectResponse | None:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return None  # caller returns 403
    return None


async def _api_error(resp: object) -> str:
    """Extract a human-readable error from an API response."""
    try:
        detail = resp.json().get("detail", f"API error: HTTP {resp.status_code}")  # type: ignore[union-attr]
    except Exception:
        detail = f"API error: HTTP {getattr(resp, 'status_code', '?')}"
    return str(detail)


# ---------------------------------------------------------------------------
# Landing — GET /admin/imports/
# ---------------------------------------------------------------------------


@router.get("/admin/imports", response_class=HTMLResponse, response_model=None)
@router.get("/admin/imports/", response_class=HTMLResponse, response_model=None)
async def imports_landing(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the imports landing page."""
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


# ===========================================================================
# Bank statement import (bank_csv / bank_ofx)
# ===========================================================================


@router.get("/admin/imports/bank", response_class=HTMLResponse, response_model=None)
async def imports_bank_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the bank statement upload form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    bank_accounts: list[dict] = []
    error: str | None = None

    # A bank statement line's ``account_id`` is a foreign key to the CHART OF
    # ACCOUNTS (accounts.id), and the reconcile screen keys off that same set,
    # so the import account picker MUST offer the reconcilable ledger accounts
    # — NOT the separate BankAccount payment-details entity (/api/v1/bank_accounts),
    # which is empty on a fresh install and whose ids are not valid BSL targets.
    # (The old /api/v1/bank-accounts call also 404'd — hyphen vs the engine's
    # /api/v1/reconciliation/accounts path — so the picker was always empty and
    # import was impossible.) reconciliation/accounts returns a bare list of
    # {id, code, name}.
    async with api_client(request) as client:
        resp = await client.get("/api/v1/reconciliation/accounts")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.is_success:
            payload = resp.json()
            bank_accounts = payload if isinstance(payload, list) else payload.get("items", [])
        else:
            error = f"Could not load accounts: HTTP {resp.status_code}"

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


@router.post("/admin/imports/bank/start", response_model=None)
async def imports_bank_start(request: Request) -> RedirectResponse:
    """Start a bank_csv/bank_ofx wizard then redirect to the upload step."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)  # type: ignore[return-value]

    form_data = await request.form()
    account_id = str(form_data.get("account_id", ""))
    if not account_id:
        request.session["flash"] = "Bank account is required."
        return RedirectResponse(url="/admin/imports/bank", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/imports/wizards",
            json={"kind": "bank_csv", "initial": {"account_id": account_id}},
        )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        request.session["flash"] = await _api_error(resp)
        return RedirectResponse(url="/admin/imports/bank", status_code=303)

    wizard_id = resp.json()["wizard_id"]
    request.session[_WIZARD_KEY] = wizard_id
    request.session[_WIZARD_KIND_KEY] = "bank_csv"
    return RedirectResponse(url="/admin/imports/bank/upload", status_code=303)


@router.get("/admin/imports/bank/upload", response_class=HTMLResponse, response_model=None)
async def imports_bank_upload_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Show the file upload step for an in-progress bank wizard."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    wizard_id = request.session.get(_WIZARD_KEY)
    if not wizard_id:
        request.session["flash"] = "No active import session. Please start again."
        return RedirectResponse(url="/admin/imports/bank", status_code=303)

    flash = request.session.pop("flash", None)
    return _TEMPLATES.TemplateResponse(
        request,
        "imports/bank_preview.html",
        {"wizard_id": wizard_id, "flash": flash, "proxy_html": None, "error": None, "account_id": None},
    )


@router.post("/admin/imports/bank/preview", response_class=HTMLResponse, response_model=None)
async def imports_bank_preview(request: Request) -> HTMLResponse | RedirectResponse:
    """Upload bank CSV/OFX file, step the wizard, show a preview.

    This endpoint accepts multipart file upload, reads the raw content, and
    posts it to the wizard step endpoint so the state is recorded.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    account_id = str(form_data.get("account_id", ""))
    file_field = form_data.get("file")

    # Check for required CSRF token in multipart forms.
    from saebooks_web.security import verify_csrf_form
    await verify_csrf_form(request)

    raw: str = ""
    if hasattr(file_field, "read"):
        content = await file_field.read()  # type: ignore[union-attr]
        raw = content.decode("utf-8-sig", errors="replace")

    # If no in-session wizard, start one now (convenience path).
    wizard_id = request.session.get(_WIZARD_KEY)
    async with api_client(request) as client:
        if not wizard_id:
            resp = await client.post(
                "/api/v1/imports/wizards",
                json={"kind": "bank_csv", "initial": {"account_id": account_id}},
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if not resp.is_success:
                error = await _api_error(resp)
                return _TEMPLATES.TemplateResponse(
                    request,
                    "imports/bank.html",
                    {"bank_accounts": [], "error": error},
                    status_code=400,
                )
            wizard_id = resp.json()["wizard_id"]
            request.session[_WIZARD_KEY] = wizard_id

        # Step the wizard with the raw content.
        step_resp = await client.post(
            f"/api/v1/imports/wizards/{wizard_id}/step",
            json={"step": 0, "patch": {"raw": raw, "account_id": account_id, "_completed": True}},
        )

    if step_resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not step_resp.is_success:
        error = await _api_error(step_resp)
        return _TEMPLATES.TemplateResponse(
            request,
            "imports/bank.html",
            {"bank_accounts": [], "error": error},
            status_code=400,
        )

    # Build a simple preview from the raw content (line count, first 10 rows).
    lines_info = [ln.strip() for ln in raw.splitlines() if ln.strip()][:12]
    proxy_html = (
        f"<p><strong>Preview:</strong> {len(lines_info)} rows detected.</p>"
        f"<pre>{'\\n'.join(lines_info[:10])}</pre>"
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "imports/bank_preview.html",
        {
            "proxy_html": proxy_html,
            "account_id": account_id,
            "wizard_id": wizard_id,
            "error": None,
        },
    )


@router.post("/admin/imports/bank/apply", response_class=HTMLResponse, response_model=None)
async def imports_bank_apply(request: Request) -> HTMLResponse | RedirectResponse:
    """Commit the bank wizard — calls POST /api/v1/imports/wizards/{id}/commit."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    wizard_id = str(form_data.get("wizard_id", "")) or request.session.get(_WIZARD_KEY, "")

    # Legacy path: if raw + account_id posted directly (old form submit),
    # start a fresh wizard and commit immediately.
    raw = str(form_data.get("raw", ""))
    account_id = str(form_data.get("account_id", ""))

    async with api_client(request) as client:
        if not wizard_id and raw and account_id:
            # Start wizard
            resp = await client.post(
                "/api/v1/imports/wizards",
                json={"kind": "bank_csv", "initial": {"account_id": account_id}},
            )
            if not resp.is_success:
                error = await _api_error(resp)
                request.session["flash"] = error
                return RedirectResponse(url="/admin/imports/bank", status_code=303)
            wizard_id = resp.json()["wizard_id"]
            # Step with raw content.
            await client.post(
                f"/api/v1/imports/wizards/{wizard_id}/step",
                json={"step": 0, "patch": {"raw": raw, "account_id": account_id, "_completed": True}},
            )

        if not wizard_id:
            request.session["flash"] = "No active import session."
            return RedirectResponse(url="/admin/imports/bank", status_code=303)

        resp = await client.post(f"/api/v1/imports/wizards/{wizard_id}/commit")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        error = await _api_error(resp)
        request.session["flash"] = error
        request.session.pop(_WIZARD_KEY, None)
        return RedirectResponse(url="/admin/imports/bank", status_code=303)

    result = resp.json()
    request.session.pop(_WIZARD_KEY, None)
    request.session.pop(_WIZARD_KIND_KEY, None)

    return _TEMPLATES.TemplateResponse(
        request,
        "imports/bank_done.html",
        {
            "inserted": result.get("inserted", 0),
            "total": result.get("total", 0),
            "account_id": account_id,
            "proxy_html": None,
        },
    )


# ===========================================================================
# Chart of accounts import
# ===========================================================================


@router.get("/admin/imports/coa", response_class=HTMLResponse, response_model=None)
async def imports_coa_form(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the CoA import upload form."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    flash = request.session.pop("flash", None)
    params = dict(request.query_params)
    return _TEMPLATES.TemplateResponse(
        request,
        "imports/coa.html",
        {
            "flash": flash,
            "result": params if params else None,
        },
    )


@router.post("/admin/imports/coa/preview", response_class=HTMLResponse, response_model=None)
async def imports_coa_preview(request: Request) -> HTMLResponse | RedirectResponse:
    """Upload CoA CSV via wizard step, show diff preview."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    from saebooks_web.security import verify_csrf_form
    await verify_csrf_form(request)

    file_field = form_data.get("file")
    raw: str = ""
    if hasattr(file_field, "read"):
        content = await file_field.read()  # type: ignore[union-attr]
        raw = content.decode("utf-8-sig", errors="replace")

    async with api_client(request) as client:
        # Start wizard
        resp = await client.post(
            "/api/v1/imports/wizards",
            json={"kind": "coa", "initial": {}},
        )
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if not resp.is_success:
            error = await _api_error(resp)
            return _TEMPLATES.TemplateResponse(
                request, "imports/coa.html", {"error": error}, status_code=400
            )
        wizard_id = resp.json()["wizard_id"]
        request.session[_WIZARD_KEY] = wizard_id
        request.session[_WIZARD_KIND_KEY] = "coa"

        # Step with raw content.
        step_resp = await client.post(
            f"/api/v1/imports/wizards/{wizard_id}/step",
            json={"step": 0, "patch": {"raw": raw, "_completed": True}},
        )

    if step_resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not step_resp.is_success:
        error = await _api_error(step_resp)
        return _TEMPLATES.TemplateResponse(
            request, "imports/coa.html", {"error": error}, status_code=400
        )

    # Show a lightweight preview (row count).
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    header = lines[0] if lines else ""
    data_rows = lines[1:] if len(lines) > 1 else []
    proxy_html = (
        f"<p><strong>Preview:</strong> {len(data_rows)} account rows to import.</p>"
        f"<pre>{header}\n" + "\n".join(data_rows[:10]) + "</pre>"
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "imports/coa_preview.html",
        {
            "proxy_html": proxy_html,
            "wizard_id": wizard_id,
            "error": None,
        },
    )


@router.post("/admin/imports/coa/apply", response_model=None)
async def imports_coa_apply(request: Request) -> RedirectResponse:
    """Commit CoA wizard — calls POST /api/v1/imports/wizards/{id}/commit."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)  # type: ignore[return-value]

    form_data = await request.form()
    wizard_id = str(form_data.get("wizard_id", "")) or request.session.get(_WIZARD_KEY, "")
    raw = str(form_data.get("raw", ""))
    archive_removed = bool(form_data.get("archive_removed", ""))

    async with api_client(request) as client:
        if not wizard_id and raw:
            # Legacy direct-form path: start + step + commit.
            resp = await client.post(
                "/api/v1/imports/wizards",
                json={"kind": "coa", "initial": {}},
            )
            if not resp.is_success:
                request.session["flash"] = await _api_error(resp)
                return RedirectResponse(url="/admin/imports/coa", status_code=303)
            wizard_id = resp.json()["wizard_id"]
            await client.post(
                f"/api/v1/imports/wizards/{wizard_id}/step",
                json={"step": 0, "patch": {"raw": raw, "archive_removed": archive_removed, "_completed": True}},
            )
        elif wizard_id:
            # If archive_removed is toggled after preview, patch it in.
            await client.post(
                f"/api/v1/imports/wizards/{wizard_id}/step",
                json={"step": 1, "patch": {"archive_removed": archive_removed}},
            )

        if not wizard_id:
            request.session["flash"] = "No active import session."
            return RedirectResponse(url="/admin/imports/coa", status_code=303)

        resp = await client.post(f"/api/v1/imports/wizards/{wizard_id}/commit")

    request.session.pop(_WIZARD_KEY, None)
    request.session.pop(_WIZARD_KIND_KEY, None)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        request.session["flash"] = await _api_error(resp)
        return RedirectResponse(url="/admin/imports/coa", status_code=303)

    result = resp.json()
    query = "&".join(f"{k}={v}" for k, v in result.items() if isinstance(v, (int, str)))
    return RedirectResponse(f"/admin/imports/coa?{query}", status_code=303)
