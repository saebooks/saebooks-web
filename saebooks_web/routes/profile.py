"""User profile — view and change password.

GET  /profile          — render form pre-filled from /auth/me
POST /profile/password — change password (current + new + confirm)
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
    return request.session.get("api_token")


@router.get("/profile", response_class=HTMLResponse, response_model=None)
async def profile_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    profile: dict = {}
    error: str | None = None

    async with api_client(request) as client:
        resp = await client.get("/api/v1/auth/me")

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    elif resp.is_success:
        profile = resp.json()
    else:
        error = f"Could not load profile (HTTP {resp.status_code})"

    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "profile/index.html",
        {"profile": profile, "flash": flash, "error": error, "pw_error": None},
    )


@router.post("/profile/password", response_model=None)
async def change_password(request: Request) -> RedirectResponse | HTMLResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    current = str(form_data.get("current_password", ""))
    new_pw = str(form_data.get("new_password", ""))
    confirm = str(form_data.get("confirm_password", ""))

    if new_pw != confirm:
        profile: dict = {}
        async with api_client(request) as client:
            me = await client.get("/api/v1/auth/me")
        if me.is_success:
            profile = me.json()
        return _TEMPLATES.TemplateResponse(
            request,
            "profile/index.html",
            {"profile": profile, "flash": None, "error": None, "pw_error": "New passwords do not match."},
            status_code=422,
        )

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": current, "new_password": new_pw},
        )

    if resp.status_code == 401:
        if "current password" in (resp.json().get("detail", "") or "").lower():
            profile = {}
            async with api_client(request) as client:
                me = await client.get("/api/v1/auth/me")
            if me.is_success:
                profile = me.json()
            return _TEMPLATES.TemplateResponse(
                request,
                "profile/index.html",
                {"profile": profile, "flash": None, "error": None, "pw_error": "Current password is incorrect."},
                status_code=422,
            )
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 422:
        detail = resp.json().get("detail", "Password change failed.")
        profile = {}
        async with api_client(request) as client:
            me = await client.get("/api/v1/auth/me")
        if me.is_success:
            profile = me.json()
        return _TEMPLATES.TemplateResponse(
            request,
            "profile/index.html",
            {"profile": profile, "flash": None, "error": None, "pw_error": detail},
            status_code=422,
        )

    if not resp.is_success:
        request.session["flash"] = f"Password change failed (HTTP {resp.status_code})."
        return RedirectResponse(url="/profile", status_code=303)

    request.session["flash"] = "Password changed successfully."
    return RedirectResponse(url="/profile", status_code=303)
