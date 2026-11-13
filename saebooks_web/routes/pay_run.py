"""``/pay-run`` -- retired (gap P1-60, batch 9).

This was the old creditor ABA screen. It sat in the menu as "Pay Run" next
to the real wages pay runs and either said payroll was not automated or
errored. Wages pay runs live at ``/payroll/pay-runs`` (``routes/wages.py``);
every old ``/pay-run`` address now redirects there so bookmarks keep working.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

_NEW = "/payroll/pay-runs"


def _go(request: Request, url: str) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url=url, status_code=303)


@router.get("/pay-run", response_model=None)
@router.get("/pay-run/new", response_model=None)
async def pay_run_index(request: Request) -> RedirectResponse:
    return _go(request, _NEW)


@router.get("/pay-run/{pay_run_id}", response_model=None)
async def pay_run_detail(request: Request, pay_run_id: UUID) -> RedirectResponse:
    return _go(request, f"{_NEW}/{pay_run_id}")


@router.post("/pay-run/{rest:path}", response_model=None)
@router.post("/pay-run", response_model=None)
async def pay_run_post(request: Request, rest: str = "") -> RedirectResponse:
    if request.session.get("api_token"):
        request.session["flash"] = (
            "The old Pay Run screen has gone. Wages pay runs are here."
        )
    return _go(request, _NEW)
