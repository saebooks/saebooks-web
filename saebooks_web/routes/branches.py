"""Branches list, create, edit, archive — admin page for managing the
per-company sub-divisional tags introduced by API migration 0134.

GET  /branches           — list page
GET  /branches/new       — create form
POST /branches/new       — submit to /api/v1/branches; redirect on 201
GET  /branches/{id}/edit — pre-populated edit form
POST /branches/{id}/edit — PATCH to API; redirect on 200, banner on 409
POST /branches/{id}/archive — DELETE to API; redirect to list

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    if request.session.get("api_token"):
        return None
    return "/login"


@router.get("/branches", response_class=HTMLResponse, response_model=None)
async def list_branches(request: Request) -> HTMLResponse | RedirectResponse:
    redirect = _require_auth(request)
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)

    branches: list[dict[str, Any]] = []
    async with api_client(request) as client:
        resp = await client.get("/api/v1/branches", params={"include_archived": True, "page": 1, "page_size": 200})
        if resp.status_code == 200:
            branches = resp.json().get("items", [])

    return _TEMPLATES.TemplateResponse(
        request,
        "branches/list.html",
        {"branches": branches},
    )


@router.get("/branches/new", response_class=HTMLResponse, response_model=None)
async def new_branch_form(request: Request) -> HTMLResponse | RedirectResponse:
    redirect = _require_auth(request)
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "branches/new.html",
        {"branch": None, "errors": None, "form": {}},
    )


@router.post("/branches/new", response_class=HTMLResponse, response_model=None)
async def create_branch(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    is_default: bool = Form(default=False),
) -> HTMLResponse | RedirectResponse:
    redirect = _require_auth(request)
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)

    payload = {"code": code.strip(), "name": name.strip(), "is_default": bool(is_default)}
    async with api_client(request) as client:
        resp = await client.post("/api/v1/branches", json=payload)

    if resp.status_code == 201:
        return RedirectResponse(url="/branches", status_code=303)

    # Show error banner
    err = "Unknown error"
    try:
        err = resp.json().get("detail", err)
    except Exception:
        pass
    return _TEMPLATES.TemplateResponse(
        request,
        "branches/new.html",
        {"branch": None, "errors": err, "form": payload},
        status_code=resp.status_code,
    )


@router.get("/branches/{branch_id}/edit", response_class=HTMLResponse, response_model=None)
async def edit_branch_form(
    request: Request, branch_id: str
) -> HTMLResponse | RedirectResponse:
    redirect = _require_auth(request)
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/branches/{branch_id}")
    if resp.status_code != 200:
        return RedirectResponse(url="/branches", status_code=303)
    branch = resp.json()
    return _TEMPLATES.TemplateResponse(
        request,
        "branches/edit.html",
        {"branch": branch, "errors": None},
    )


@router.post("/branches/{branch_id}/edit", response_class=HTMLResponse, response_model=None)
async def update_branch(
    request: Request,
    branch_id: str,
    name: str = Form(...),
    is_default: bool = Form(default=False),
) -> HTMLResponse | RedirectResponse:
    redirect = _require_auth(request)
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    payload = {"name": name.strip(), "is_default": bool(is_default)}
    async with api_client(request) as client:
        resp = await client.patch(f"/api/v1/branches/{branch_id}", json=payload)
    if resp.status_code == 200:
        return RedirectResponse(url="/branches", status_code=303)
    # error
    branch = {"id": branch_id, "code": "?", **payload}
    err = "Unknown error"
    try:
        err = resp.json().get("detail", err)
    except Exception:
        pass
    return _TEMPLATES.TemplateResponse(
        request,
        "branches/edit.html",
        {"branch": branch, "errors": err},
        status_code=resp.status_code,
    )


@router.post("/branches/{branch_id}/archive")
async def archive_branch(request: Request, branch_id: str) -> RedirectResponse:
    redirect = _require_auth(request)
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    async with api_client(request) as client:
        await client.delete(f"/api/v1/branches/{branch_id}")
    return RedirectResponse(url="/branches", status_code=303)
