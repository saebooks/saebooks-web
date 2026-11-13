"""Recurring bills — /recurring-bills (b11, P1-13).

Rent, subscriptions and other supplier costs that repeat. Same pages as
recurring invoices: list, new, detail, edit, pause/resume, archive, and
"Make bill now". The engine's daily scheduler makes due bills on its own.

All data comes from the engine's ``/api/v1/recurring_bills``.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.form_helpers import parse_lines as _parse_lines

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_FREQUENCIES = ["WEEKLY", "FORTNIGHTLY", "MONTHLY", "QUARTERLY", "YEARLY"]


def _authed(request: Request) -> bool:
    return bool(request.session.get("api_token"))


def _detail_message(resp) -> str:  # type: ignore[no-untyped-def]
    try:
        detail = resp.json().get("detail")
    except Exception:
        return f"HTTP {resp.status_code}"
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        first = detail[0]
        loc = [p for p in first.get("loc", []) if p != "body"]
        field = f"{loc[0]}: " if loc else ""
        return f"{field}{first.get('msg', 'Invalid value')}"
    return f"HTTP {resp.status_code}"


async def _dropdowns(client) -> tuple[list[dict], list[dict], list[dict]]:  # type: ignore[no-untyped-def]
    suppliers: list[dict] = []
    for ctype in ("SUPPLIER", "BOTH", "CONTRACTOR"):
        r = await client.get("/api/v1/contacts", params={"type": ctype, "limit": 200, "offset": 0})
        if r.is_success:
            suppliers.extend(r.json().get("items", []))
    suppliers.sort(key=lambda c: (c.get("name") or "").lower())
    accounts: list[dict] = []
    r = await client.get("/api/v1/accounts", params={"limit": 200, "offset": 0})
    if r.is_success:
        accounts = r.json().get("items", [])
    tax_codes: list[dict] = []
    r = await client.get("/api/v1/tax_codes", params={"limit": 100, "offset": 0})
    if r.is_success:
        tax_codes = r.json().get("items", [])
    return suppliers, accounts, tax_codes


def _payload(form: dict[str, str]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for f in ("contact_id", "name", "frequency", "next_run", "supplier_reference", "notes"):
        val = (form.get(f) or "").strip()
        if val:
            payload[f] = val
    payload["end_date"] = (form.get("end_date") or "").strip() or None
    for f in ("anchor_day", "due_days"):
        val = (form.get(f) or "").strip()
        if val:
            with contextlib.suppress(ValueError):
                payload[f] = int(val)
    payload["auto_post"] = bool(form.get("auto_post"))
    payload["catch_up"] = not bool(form.get("skip_missed"))
    payload["lines"] = _parse_lines(form)
    return payload


async def _render_form(
    request: Request, *, form: dict, errors: dict, rb: dict | None, status_code: int = 200
) -> HTMLResponse:
    async with api_client(request) as client:
        suppliers, accounts, tax_codes = await _dropdowns(client)
    raw_lines = form.get("_lines") or _parse_lines(form) if form else []
    lines = [{"index": i, **ln} for i, ln in enumerate(raw_lines)] or [{"index": 0}]
    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_bills/form.html",
        {
            "rb": rb,
            "form": form,
            "errors": errors,
            "contacts": suppliers,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "lines": lines,
            "line_count": len(lines),
            "frequencies": _FREQUENCIES,
        },
        status_code=status_code,
    )


@router.get("/recurring-bills", response_class=HTMLResponse, response_model=None)
async def recurring_bills_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    archived = request.query_params.get("archived") == "1"
    status = (request.query_params.get("status") or "").upper() or None
    error = None
    items: list[dict] = []
    names: dict[str, str] = {}
    async with api_client(request) as client:
        params: dict[str, object] = {"page": 1, "page_size": 200, "archived": archived}
        if status in ("ACTIVE", "PAUSED", "ENDED"):
            params["status"] = status
        r = await client.get("/api/v1/recurring_bills", params=params)
        if r.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if r.is_success:
            items = r.json().get("items", [])
        else:
            error = _detail_message(r)
        ids = {i["contact_id"] for i in items}
        for cid in ids:
            c = await client.get(f"/api/v1/contacts/{cid}")
            if c.is_success:
                names[cid] = c.json().get("name", "")
    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_bills/list.html",
        {
            "items": items,
            "names": names,
            "archived": archived,
            "status": status,
            "error": error,
            "flash": request.session.pop("flash", None),
            "today_iso": date.today().isoformat(),
        },
    )


@router.get("/recurring-bills/new", response_class=HTMLResponse, response_model=None)
async def recurring_bill_new(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = {"due_days": "30", "next_run": date.today().isoformat(), "frequency": "MONTHLY"}
    return await _render_form(request, form=form, errors={}, rb=None)


@router.post("/recurring-bills/new", response_class=HTMLResponse, response_model=None)
async def recurring_bill_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = {k: v for k, v in (await request.form()).items()}  # type: ignore[misc]
    async with api_client(request) as client:
        r = await client.post("/api/v1/recurring_bills", json=_payload(form))
    if r.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if r.status_code == 201:
        request.session["flash"] = "Recurring bill saved."
        return RedirectResponse(url=f"/recurring-bills/{r.json()['id']}", status_code=303)
    return await _render_form(
        request, form=form, errors={"__all__": _detail_message(r)}, rb=None, status_code=422
    )


@router.get("/recurring-bills/{rb_id}/edit", response_class=HTMLResponse, response_model=None)
async def recurring_bill_edit(request: Request, rb_id: str) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/recurring_bills/{rb_id}")
    if not r.is_success:
        return RedirectResponse(url="/recurring-bills", status_code=303)
    rb = r.json()
    form = {
        k: ("" if rb.get(k) is None else str(rb.get(k)))
        for k in ("name", "contact_id", "frequency", "next_run", "end_date", "anchor_day",
                  "due_days", "supplier_reference", "notes")
    }
    form["auto_post"] = rb.get("auto_post", False)
    form["skip_missed"] = not rb.get("catch_up", True)
    form["version"] = str(rb.get("version", ""))
    form["_lines"] = [
        {k: ln.get(k) for k in ("description", "account_id", "tax_code_id", "quantity", "unit_price")}
        for ln in rb.get("lines", [])
    ]
    return await _render_form(request, form=form, errors={}, rb=rb)


@router.post("/recurring-bills/{rb_id}/edit", response_class=HTMLResponse, response_model=None)
async def recurring_bill_update(request: Request, rb_id: str) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = {k: v for k, v in (await request.form()).items()}  # type: ignore[misc]
    payload = _payload(form)
    async with api_client(request) as client:
        r = await client.patch(
            f"/api/v1/recurring_bills/{rb_id}", json=payload,
            headers={"If-Match": form.get("version", "")},
        )
        current = await client.get(f"/api/v1/recurring_bills/{rb_id}")
    if r.is_success:
        request.session["flash"] = "Recurring bill updated."
        return RedirectResponse(url=f"/recurring-bills/{rb_id}", status_code=303)
    msg = (
        "Someone else changed this recurring bill. Reload and try again."
        if r.status_code == 409 else _detail_message(r)
    )
    return await _render_form(
        request, form=form, errors={"__all__": msg},
        rb=current.json() if current.is_success else None, status_code=422,
    )


async def _action(request: Request, rb_id: str, verb: str, method: str = "POST") -> RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    version = str(form.get("version", ""))
    path = f"/api/v1/recurring_bills/{rb_id}" + ("" if verb == "archive" else f"/{verb}")
    async with api_client(request) as client:
        if method == "DELETE":
            r = await client.delete(path, headers={"If-Match": version})
        else:
            r = await client.post(path, headers={"If-Match": version})
    labels = {
        "pause": "Paused.", "resume": "Resumed.", "end": "Ended.", "archive": "Archived.",
        "generate": "Bill made.",
    }
    if r.is_success:
        request.session["flash"] = labels.get(verb, "Done.")
        if verb == "generate":
            bill_id = r.json().get("bill_id")
            if bill_id:
                return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)
        if verb == "archive":
            return RedirectResponse(url="/recurring-bills", status_code=303)
    else:
        request.session["flash"] = f"Could not {verb}: {_detail_message(r)}"
    return RedirectResponse(url=f"/recurring-bills/{rb_id}", status_code=303)


@router.post("/recurring-bills/{rb_id}/pause", response_model=None)
async def recurring_bill_pause(request: Request, rb_id: str) -> RedirectResponse:
    return await _action(request, rb_id, "pause")


@router.post("/recurring-bills/{rb_id}/resume", response_model=None)
async def recurring_bill_resume(request: Request, rb_id: str) -> RedirectResponse:
    return await _action(request, rb_id, "resume")


@router.post("/recurring-bills/{rb_id}/end", response_model=None)
async def recurring_bill_end(request: Request, rb_id: str) -> RedirectResponse:
    return await _action(request, rb_id, "end")


@router.post("/recurring-bills/{rb_id}/archive", response_model=None)
async def recurring_bill_archive(request: Request, rb_id: str) -> RedirectResponse:
    return await _action(request, rb_id, "archive", method="DELETE")


@router.post("/recurring-bills/{rb_id}/generate", response_model=None)
async def recurring_bill_generate(request: Request, rb_id: str) -> RedirectResponse:
    return await _action(request, rb_id, "generate")


@router.get("/recurring-bills/{rb_id}", response_class=HTMLResponse, response_model=None)
async def recurring_bill_detail(request: Request, rb_id: str) -> HTMLResponse | RedirectResponse:
    if not _authed(request):
        return RedirectResponse(url="/login", status_code=303)
    supplier = ""
    accounts: dict[str, str] = {}
    async with api_client(request) as client:
        r = await client.get(f"/api/v1/recurring_bills/{rb_id}")
        if r.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if not r.is_success:
            return _TEMPLATES.TemplateResponse(
                request, "recurring_bills/detail.html",
                {"rb": None, "error": "Recurring bill not found", "flash": None},
                status_code=404 if r.status_code == 404 else r.status_code,
            )
        rb = r.json()
        c = await client.get(f"/api/v1/contacts/{rb['contact_id']}")
        if c.is_success:
            supplier = c.json().get("name", "")
        a = await client.get("/api/v1/accounts", params={"limit": 500, "offset": 0})
        if a.is_success:
            accounts = {
                x["id"]: f"{x.get('code') or ''} {x.get('name') or ''}".strip()
                for x in a.json().get("items", [])
            }
    return _TEMPLATES.TemplateResponse(
        request,
        "recurring_bills/detail.html",
        {
            "rb": rb,
            "supplier": supplier,
            "accounts": accounts,
            "error": None,
            "flash": request.session.pop("flash", None),
            "today_iso": date.today().isoformat(),
            "nonce": str(uuid.uuid4()),
        },
    )
