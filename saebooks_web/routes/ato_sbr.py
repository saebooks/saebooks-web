"""ATO SBR Machine Credential wizard views — Lane D cycle 54.

Route map
---------
GET  /admin/ato-sbr           — wizard showing keystore status + off-system steps
POST /admin/ato-sbr/keystore  — upload keystore.xml file + password, proxy to API
POST /admin/ato-sbr/ssid      — save SSID, proxy to API
POST /admin/ato-sbr/confirm   — confirm an off-system step, proxy to API
POST /admin/ato-sbr/test      — run environment smoke test, proxy to API
POST /admin/ato-sbr/clear     — clear config, proxy to API

API endpoints consumed:
- GET  /admin/ato-sbr           → HTML wizard page (proxied or own template)
- POST /admin/ato-sbr/confirm   (form: step=...)
- POST /admin/ato-sbr/keystore  (multipart: file=..., password=...)
- POST /admin/ato-sbr/ssid      (form: ssid=...)
- POST /admin/ato-sbr/test      (form: environment=...)
- POST /admin/ato-sbr/clear     (no body)

All POST endpoints redirect back to GET /admin/ato-sbr with message= or error=
query params, which the GET handler surfaces in the template.

Feature-flag gated (FLAG_ATO_SBR) in the upstream API — if the feature is off
the API returns 404. The web layer surfaces a friendly message in that case.

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
    """True if session is SAE staff or tenant admin."""
    role = request.session.get("user_role", "")
    is_staff = bool(request.session.get("is_sae_staff"))
    return is_staff or role == "admin"


# ---------------------------------------------------------------------------
# GET /admin/ato-sbr — wizard landing page
# ---------------------------------------------------------------------------


@router.get("/admin/ato-sbr", response_class=HTMLResponse, response_model=None)
async def ato_sbr_index(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    test: str | None = None,
    test_env: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the ATO SBR onboarding wizard.

    Fetches current config status from the upstream API.  If the feature flag
    is disabled (404) renders a friendly not-available page.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    config: dict | None = None
    feature_disabled = False
    api_error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/admin/ato-sbr")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    elif resp.status_code == 404:
        feature_disabled = True
    elif not resp.is_success:
        api_error = f"API error: HTTP {resp.status_code}"
    else:
        # The API returns HTML; we render our own template and just check the
        # upstream is reachable.  Config details aren't available as JSON here.
        config = {"available": True}

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "ato_sbr/index.html",
        {
            "config": config,
            "feature_disabled": feature_disabled,
            "api_error": api_error,
            "message": message or flash,
            "error": error,
            "test_badge": test,
            "test_env": test_env,
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/keystore — upload keystore file
# ---------------------------------------------------------------------------


@router.get("/admin/ato-sbr/keystore", response_model=None)
@router.get("/admin/ato-sbr/confirm", response_model=None)
@router.get("/admin/ato-sbr/ssid", response_model=None)
@router.get("/admin/ato-sbr/test", response_model=None)
@router.get("/admin/ato-sbr/clear", response_model=None)
async def ato_sbr_action_redirect(
    request: Request,
) -> RedirectResponse | HTMLResponse:
    """Redirect stray GETs back to the wizard.

    Authentik forward-auth redirects back to the original URL after SSO but
    using GET (the POST body is lost in the redirect). Without this handler
    the user sees a bare 405 JSON error. Bounce them to the wizard landing
    page instead so they can re-submit the form.

    Auth is checked here first so the response signal is consistent with
    every other /admin/* route: anonymous -> 303 /login, authenticated but
    unauthorised -> 403 directly. Without the check, an unauthorised user
    would get 303 -> /admin/ato-sbr -> 403 (two hops, inconsistent).
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)
    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


@router.post("/admin/ato-sbr/keystore", response_model=None)
async def ato_sbr_upload_keystore(request: Request) -> RedirectResponse:
    """Upload keystore.xml + password to the API."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    # Multipart route — explicit CSRF token check after parsing the form.
    from saebooks_web.security import verify_csrf_form  # noqa: PLC0415
    await verify_csrf_form(request)
    password = str(form_data.get("password", ""))
    file_field = form_data.get("file")

    files: dict | None = None
    if hasattr(file_field, "read"):
        content = await file_field.read()  # type: ignore[union-attr]
        filename = getattr(file_field, "filename", "keystore.xml") or "keystore.xml"
        files = {"file": (filename, content, "application/xml")}

    async with api_client(request) as client:
        if files:
            resp = await client.post(
                "/admin/ato-sbr/keystore",
                data={"password": password},
                files=files,
                follow_redirects=False,
            )
        else:
            resp = await client.post(
                "/admin/ato-sbr/keystore",
                data={"password": password},
                follow_redirects=False,
            )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # The API redirects to /admin/ato-sbr?message=... or ?error=...
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "/admin/ato-sbr")
        # Remap to our path.
        if location.startswith("/admin/ato-sbr"):
            return RedirectResponse(url=location, status_code=303)

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/ssid — save SSID
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/ssid", response_model=None)
async def ato_sbr_save_ssid(request: Request) -> RedirectResponse:
    """Save the ATO Software Service ID."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {"ssid": str(form_data.get("ssid", ""))}

    async with api_client(request) as client:
        resp = await client.post(
            "/admin/ato-sbr/ssid",
            data=form,
            follow_redirects=False,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "/admin/ato-sbr")
        if location.startswith("/admin/ato-sbr"):
            return RedirectResponse(url=location, status_code=303)

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/confirm — confirm off-system step
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/confirm", response_model=None)
async def ato_sbr_confirm_step(request: Request) -> RedirectResponse:
    """Confirm an off-system onboarding step (myGovID, RAM link, etc.)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {"step": str(form_data.get("step", ""))}

    async with api_client(request) as client:
        resp = await client.post(
            "/admin/ato-sbr/confirm",
            data=form,
            follow_redirects=False,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "/admin/ato-sbr")
        if location.startswith("/admin/ato-sbr"):
            return RedirectResponse(url=location, status_code=303)

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/test — smoke test environment
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/test", response_model=None)
async def ato_sbr_test(request: Request) -> RedirectResponse:
    """Run a smoke test against the selected environment (EVTE or PROD)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    form: dict[str, str] = {"environment": str(form_data.get("environment", ""))}

    async with api_client(request) as client:
        resp = await client.post(
            "/admin/ato-sbr/test",
            data=form,
            follow_redirects=False,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "/admin/ato-sbr")
        if location.startswith("/admin/ato-sbr"):
            return RedirectResponse(url=location, status_code=303)

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/clear — clear config
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/clear", response_model=None)
async def ato_sbr_clear(request: Request) -> RedirectResponse:
    """Clear the ATO SBR config (wipes keystore, SSID, and confirmations)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    async with api_client(request) as client:
        resp = await client.post(
            "/admin/ato-sbr/clear",
            follow_redirects=False,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "/admin/ato-sbr")
        if location.startswith("/admin/ato-sbr"):
            return RedirectResponse(url=location, status_code=303)

    return RedirectResponse(url="/admin/ato-sbr?message=config+cleared", status_code=303)
