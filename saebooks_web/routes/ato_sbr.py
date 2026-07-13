"""ATO SBR Machine Credential wizard views — Cat-C cycle (W3 rewrite).

Route map
---------
GET  /admin/ato-sbr                — wizard showing keystore status + off-system steps
POST /admin/ato-sbr/keystore       — upload keystore.xml, proxy to /api/v1/ato_sbr/keystore
POST /admin/ato-sbr/keystore/{id}/delete  — soft-delete, proxy to DELETE /api/v1/ato_sbr/keystore/{id}
POST /admin/ato-sbr/onboarding/start     — start wizard, proxy to /api/v1/ato_sbr/onboarding/wizards
POST /admin/ato-sbr/onboarding/{id}/step — advance wizard, proxy to .../wizards/{id}/step
POST /admin/ato-sbr/ping           — lodge-server ping, proxy to /api/v1/ato_sbr/ping
GET  /admin/ato-sbr/keystore       — redirect stray GETs to wizard

Auth guard: redirect to /login (303) if no session token.
Admin guard: return 403 if session role is not admin / is_sae_staff.
Feature guard: if the upstream API returns 404 (FLAG_ATO_SBR disabled),
    render a friendly "feature not available" page.

All POST endpoints proxy to the new /api/v1/ato_sbr/* endpoints and
redirect back to GET /admin/ato-sbr with message= or error= query params.
The same Jinja2 templates (``templates/ato_sbr/index.html``) are used;
the template receives ``keystore_entries`` (list), ``wizard_status``
(dict | None), and ``flash`` / ``error`` strings.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _require_auth(request: Request) -> str | None:
    """Return the session token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _require_admin(request: Request) -> bool:
    """True if session user is SAE staff or has the owner/admin role."""
    role = request.session.get("user_role", "")
    is_staff = bool(request.session.get("is_sae_staff"))
    return is_staff or role in ("owner", "admin")


def _flash(request: Request, message: str | None = None, error: str | None = None) -> None:
    """Write a flash message or error into the session for GET redirect."""
    if message:
        request.session["flash_message"] = message
    if error:
        request.session["flash_error"] = error


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    """Pop and return (message, error) from session flash."""
    msg = request.session.pop("flash_message", None)
    err = request.session.pop("flash_error", None)
    return msg, err


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

    Fetches keystore entries from /api/v1/ato_sbr/keystore.
    If the feature flag is disabled (404) renders a friendly not-available page.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    keystore_entries: list[dict] = []
    feature_disabled = False
    api_error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/api/v1/ato_sbr/keystore")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    elif resp.status_code == 404:
        feature_disabled = True
    elif not resp.is_success:
        api_error = f"API error: HTTP {resp.status_code}"
    else:
        body = resp.json()
        keystore_entries = body.get("items", [])

    # Pop any session flash values (written by redirect handlers).
    flash_msg, flash_err = _pop_flash(request)

    return _TEMPLATES.TemplateResponse(
        request,
        "ato_sbr/index.html",
        {
            "keystore_entries": keystore_entries,
            "feature_disabled": feature_disabled,
            "api_error": api_error,
            "message": message or flash_msg,
            "error": error or flash_err,
            "test_badge": test,
            "test_env": test_env,
            "active_wizard_id": request.session.get("active_wizard_id"),
            "active_wizard_step_index": request.session.get(
                "active_wizard_step_index"
            ),
            "active_wizard_step_count": request.session.get(
                "active_wizard_step_count"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Stray-GET redirect for action sub-paths
# ---------------------------------------------------------------------------


@router.get("/admin/ato-sbr/keystore", response_model=None)
@router.get("/admin/ato-sbr/onboarding/start", response_model=None)
async def ato_sbr_action_redirect(
    request: Request,
) -> RedirectResponse | HTMLResponse:
    """Redirect stray GETs back to the wizard landing page.

    The OAuth callback can redirect to the original URL via GET,
    losing the POST body. Bounce back to the wizard so the user can
    re-submit rather than seeing a bare 405.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)
    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/keystore — upload keystore file
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/keystore", response_model=None)
async def ato_sbr_upload_keystore(request: Request) -> RedirectResponse:
    """Upload keystore.xml + password to /api/v1/ato_sbr/keystore."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    from saebooks_web.security import verify_csrf_form
    await verify_csrf_form(request)

    password = str(form_data.get("password", ""))
    label = str(form_data.get("label", ""))
    file_field = form_data.get("file")

    files: dict | None = None
    if hasattr(file_field, "read"):
        content = await file_field.read()  # type: ignore[union-attr]
        filename = getattr(file_field, "filename", "keystore.xml") or "keystore.xml"
        files = {"file": (filename, content, "application/xml")}

    async with api_client(request) as client:
        if files:
            resp = await client.post(
                "/api/v1/ato_sbr/keystore",
                data={"password": password, "label": label},
                files=files,
            )
        else:
            resp = await client.post(
                "/api/v1/ato_sbr/keystore",
                data={"password": password, "label": label},
            )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        entry = resp.json()
        cn = entry.get("abn_or_name") or entry.get("label") or "unknown"
        _flash(request, message=f"Keystore uploaded ({cn})")
    elif resp.status_code == 422:
        detail = resp.json().get("detail", "Upload failed")
        _flash(request, error=str(detail)[:200])
    elif resp.status_code == 503:
        _flash(request, error="Encryption not configured on server — contact your administrator")
    else:
        _flash(request, error=f"Upload failed: HTTP {resp.status_code}")

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/keystore/{id}/delete — soft-delete keystore entry
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/keystore/{entry_id}/delete", response_model=None)
async def ato_sbr_delete_keystore(
    entry_id: str, request: Request
) -> RedirectResponse | HTMLResponse:
    """Soft-delete a keystore entry via DELETE /api/v1/ato_sbr/keystore/{id}."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/ato_sbr/keystore/{entry_id}")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 204:
        _flash(request, message="Keystore entry removed")
    elif resp.status_code == 409:
        _flash(request, error="Keystore entry is already archived")
    elif resp.status_code == 404:
        _flash(request, error="Keystore entry not found")
    else:
        _flash(request, error=f"Delete failed: HTTP {resp.status_code}")

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/onboarding/start — start a wizard
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/onboarding/start", response_model=None)
async def ato_sbr_start_wizard(request: Request) -> RedirectResponse | HTMLResponse:
    """Start an ATO SBR onboarding wizard session via /api/v1/ato_sbr/onboarding/wizards."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    flow = str(form_data.get("flow", "machine_credential"))

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/ato_sbr/onboarding/wizards",
            json={"flow": flow},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 201:
        wizard = resp.json()
        wizard_id = wizard.get("wizard_id", "")
        # Store wizard id and current step info in session.
        request.session["active_wizard_id"] = wizard_id
        request.session["active_wizard_flow"] = flow
        request.session["active_wizard_step"] = wizard.get("current_step")
        request.session["active_wizard_step_index"] = wizard.get("step_index")
        request.session["active_wizard_step_count"] = wizard.get("step_count")
        return RedirectResponse(
            url=f"/admin/ato-sbr/onboarding/{wizard_id}",
            status_code=303,
        )
    elif resp.status_code == 422:
        detail = resp.json().get("detail", "Invalid flow")
        _flash(request, error=str(detail)[:200])
    else:
        _flash(request, error=f"Could not start wizard: HTTP {resp.status_code}")

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)


# ---------------------------------------------------------------------------
# GET /admin/ato-sbr/onboarding/{wizard_id} — wizard step page
# ---------------------------------------------------------------------------


@router.get(
    "/admin/ato-sbr/onboarding/{wizard_id}",
    response_class=HTMLResponse,
    response_model=None,
)
async def ato_sbr_wizard_page(
    wizard_id: str,
    request: Request,
    error: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Render the current step of the ATO SBR onboarding wizard.

    The wizard state is read from the API (via the wizard id stored in
    the session). If the wizard has expired or is complete, redirect
    back to the landing page.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    # We don't have a GET endpoint on the wizard; reconstruct from the
    # start-wizard response stored in session, or redirect back.
    stored_id = request.session.get("active_wizard_id")
    if stored_id != wizard_id:
        _flash(request, error="Wizard session not found or expired")
        return RedirectResponse(url="/admin/ato-sbr", status_code=303)

    flash_msg, flash_err = _pop_flash(request)

    current_step = request.session.get("active_wizard_step")
    step_index = request.session.get("active_wizard_step_index")
    step_count = request.session.get("active_wizard_step_count")

    if current_step is None:
        # No step info in session (e.g. server restart) — restart wizard.
        request.session.pop("active_wizard_id", None)
        request.session.pop("active_wizard_flow", None)
        _flash(request, error="Wizard step state lost — please start again")
        return RedirectResponse(url="/admin/ato-sbr", status_code=303)

    return _TEMPLATES.TemplateResponse(
        request,
        "ato_sbr/wizard.html",
        {
            "wizard_id": wizard_id,
            "flow": request.session.get("active_wizard_flow", "machine_credential"),
            "current_step": current_step,
            "step_index": step_index,
            "step_count": step_count,
            "message": flash_msg,
            "error": error or flash_err,
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/onboarding/{wizard_id}/step — advance wizard step
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/onboarding/{wizard_id}/step", response_model=None)
async def ato_sbr_wizard_step(
    wizard_id: str, request: Request
) -> RedirectResponse | HTMLResponse:
    """Submit the current wizard step via /api/v1/ato_sbr/onboarding/wizards/{id}/step."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    # Build answers dict from all form fields except csrf_token and step.
    answers: dict = {}
    current_step = None
    for key, val in form_data.items():
        if key == "csrf_token":
            continue
        if key == "current_step":
            with contextlib.suppress(ValueError):
                current_step = int(str(val))
            continue
        # Checkboxes come through as "on" in HTML forms.
        if str(val).lower() in ("on", "true", "yes", "1"):
            answers[key] = True
        elif str(val).lower() in ("off", "false", "no", "0"):
            answers[key] = False
        else:
            answers[key] = str(val)

    headers: dict[str, str] = {}
    if current_step is not None:
        headers["If-Match"] = str(current_step)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/ato_sbr/onboarding/wizards/{wizard_id}/step",
            json={"answers": answers},
            headers=headers,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 200:
        body = resp.json()
        if body.get("status") == "complete":
            request.session.pop("active_wizard_id", None)
            request.session.pop("active_wizard_flow", None)
            request.session.pop("active_wizard_step", None)
            request.session.pop("active_wizard_step_index", None)
            request.session.pop("active_wizard_step_count", None)
            _flash(request, message="ATO SBR onboarding complete!")
            return RedirectResponse(url="/admin/ato-sbr", status_code=303)
        # Still in progress — refresh session step info, back to wizard page.
        request.session["active_wizard_step"] = body.get("current_step")
        request.session["active_wizard_step_index"] = body.get("step_index")
        request.session["active_wizard_step_count"] = body.get("step_count")
        return RedirectResponse(
            url=f"/admin/ato-sbr/onboarding/{wizard_id}",
            status_code=303,
        )
    elif resp.status_code == 409:
        body = resp.json()
        detail = body.get("detail", "Step conflict — please refresh and try again")
        _flash(request, error=str(detail)[:200])
    elif resp.status_code == 422:
        body = resp.json()
        detail = body.get("detail", "Please fill in all required fields")
        _flash(request, error=str(detail)[:200])
    elif resp.status_code in (404, 410):
        request.session.pop("active_wizard_id", None)
        request.session.pop("active_wizard_flow", None)
        request.session.pop("active_wizard_step", None)
        request.session.pop("active_wizard_step_index", None)
        request.session.pop("active_wizard_step_count", None)
        _flash(request, error="Wizard session expired — please start again")
        return RedirectResponse(url="/admin/ato-sbr", status_code=303)
    else:
        _flash(request, error=f"Step submission failed: HTTP {resp.status_code}")

    return RedirectResponse(
        url=f"/admin/ato-sbr/onboarding/{wizard_id}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# POST /admin/ato-sbr/ping — test lodge-server connection
# ---------------------------------------------------------------------------


@router.post("/admin/ato-sbr/ping", response_model=None)
async def ato_sbr_ping(request: Request) -> RedirectResponse | HTMLResponse:
    """Test lodge-server connectivity for a chosen keystore entry."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _require_admin(request):
        return HTMLResponse("Forbidden — admin role required", status_code=403)

    form_data = await request.form()
    keystore_id = str(form_data.get("keystore_id", ""))

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/ato_sbr/ping",
            json={"keystore_id": keystore_id},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.is_success:
        body = resp.json()
        if body.get("ok"):
            latency = body.get("latency_ms", "?")
            _flash(request, message=f"Lodge-server reachable (latency {latency}ms)")
        else:
            reason = body.get("reason", "unknown")
            detail = body.get("detail", "")
            if reason == "lodge_server_stub_mode":
                _flash(
                    request,
                    error="Lodge-server is in stub mode — real lodgement not yet available",
                )
            elif reason == "lodge_server_auth_error":
                _flash(request, error=f"Licence auth failed: {detail}")
            else:
                _flash(request, error=f"Ping failed ({reason}): {detail}")
    else:
        _flash(request, error=f"Ping request failed: HTTP {resp.status_code}")

    return RedirectResponse(url="/admin/ato-sbr", status_code=303)
