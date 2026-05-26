"""Party bucket pages — Suppliers / Customers / Beneficiaries (filtered
slices of /contacts) and One-off Suppliers / One-off Customers (separate
tables under /api/v1/one-off-vendors and /api/v1/one-off-customers).

These pages live under the sidebar "Contacts" dropdown. The underlying
data sources are:

- /suppliers, /customers, /beneficiaries → /api/v1/contacts with a
  contact_type filter (two-call merge for SUPPLIER+BOTH /
  CUSTOMER+BOTH; single call for BENEFICIARY).
- /one-off-suppliers → /api/v1/one-off-vendors
- /one-off-customers → /api/v1/one-off-customers
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


async def _fetch_contacts_by_types(
    request: Request,
    types: list[str],
    *,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int, str | None]:
    """Fetch contacts whose ``contact_type`` is in ``types``.

    The contacts API accepts a single contact_type filter, so for
    ``["SUPPLIER", "BOTH"]`` style buckets we fan out two calls and
    merge. Pagination is applied to the merged + name-sorted list.
    """
    merged: list[dict] = []
    error: str | None = None
    async with api_client(request) as client:
        for ct in types:
            params: dict[str, object] = {"contact_type": ct, "limit": 500, "offset": 0}
            if search:
                params["search"] = search
            try:
                resp = await client.get("/api/v1/contacts", params=params)
            except Exception as exc:  # noqa: BLE001
                error = f"Upstream error: {exc}"
                continue
            if resp.status_code != 200:
                error = f"Upstream returned {resp.status_code}"
                continue
            merged.extend(resp.json().get("items") or [])
    # Dedupe by id (BOTH would never overlap with SUPPLIER, but safe).
    seen: set[str] = set()
    deduped: list[dict] = []
    for c in merged:
        cid = c.get("id")
        if cid in seen:
            continue
        seen.add(cid)
        deduped.append(c)
    deduped.sort(key=lambda c: (c.get("name") or "").lower())
    total = len(deduped)
    page = deduped[offset : offset + limit]
    return page, total, error


async def _fetch_one_off(
    request: Request,
    endpoint: str,
    *,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int, str | None]:
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if search:
        params["search"] = search
    async with api_client(request) as client:
        try:
            resp = await client.get(endpoint, params=params)
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"Upstream error: {exc}"
    if resp.status_code == 404:
        # API endpoint not deployed yet — show empty page with a hint.
        return [], 0, "One-off API endpoint not deployed yet."
    if resp.status_code != 200:
        return [], 0, f"Upstream returned {resp.status_code}"
    body = resp.json()
    return body.get("items") or [], int(body.get("total") or 0), None


# ---------------------------------------------------------------------------
# Suppliers (SUPPLIER + BOTH)
# ---------------------------------------------------------------------------


@router.get("/suppliers", response_class=HTMLResponse, response_model=None)
async def suppliers_list(
    request: Request,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items, total, error = await _fetch_contacts_by_types(
        request, ["SUPPLIER", "BOTH"], search=search, limit=limit, offset=offset,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "parties/contacts_bucket.html",
        {
            "title": "Suppliers",
            "subtitle": "Ongoing suppliers (SUPPLIER + BOTH)",
            "icon": "truck",
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search or "",
            "error": error,
            "base_path": "/suppliers",
            "new_url": "/contacts/new?type=SUPPLIER",
            "new_label": "New supplier",
        },
    )


# ---------------------------------------------------------------------------
# Customers (CUSTOMER + BOTH)
# ---------------------------------------------------------------------------


@router.get("/customers", response_class=HTMLResponse, response_model=None)
async def customers_list(
    request: Request,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items, total, error = await _fetch_contacts_by_types(
        request, ["CUSTOMER", "BOTH"], search=search, limit=limit, offset=offset,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "parties/contacts_bucket.html",
        {
            "title": "Customers",
            "subtitle": "Ongoing customers (CUSTOMER + BOTH)",
            "icon": "user-round",
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search or "",
            "error": error,
            "base_path": "/customers",
            "new_url": "/contacts/new?type=CUSTOMER",
            "new_label": "New customer",
        },
    )


# ---------------------------------------------------------------------------
# Beneficiaries (BENEFICIARY only)
# ---------------------------------------------------------------------------


@router.get("/beneficiaries", response_class=HTMLResponse, response_model=None)
async def beneficiaries_list(
    request: Request,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items, total, error = await _fetch_contacts_by_types(
        request, ["BENEFICIARY"], search=search, limit=limit, offset=offset,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "parties/contacts_bucket.html",
        {
            "title": "Beneficiaries",
            "subtitle": "Trust / SMSF beneficiaries",
            "icon": "heart",
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search or "",
            "error": error,
            "base_path": "/beneficiaries",
            "new_url": "/contacts/new?type=BENEFICIARY",
            "new_label": "New beneficiary",
        },
    )


# ---------------------------------------------------------------------------
# One-off suppliers
# ---------------------------------------------------------------------------


@router.get("/one-off-suppliers", response_class=HTMLResponse, response_model=None)
async def one_off_suppliers_list(
    request: Request,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items, total, error = await _fetch_one_off(
        request, "/api/v1/one-off-vendors", search=search, limit=limit, offset=offset,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "parties/one_off_bucket.html",
        {
            "title": "One-off suppliers",
            "subtitle": "Cash purchases, walk-ins, COD vendors",
            "icon": "package",
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search or "",
            "error": error,
            "base_path": "/one-off-suppliers",
            "amount_column": "total_spent",
            "amount_label": "Total spent",
        },
    )


# ---------------------------------------------------------------------------
# One-off customers
# ---------------------------------------------------------------------------


@router.get("/one-off-customers", response_class=HTMLResponse, response_model=None)
async def one_off_customers_list(
    request: Request,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    items, total, error = await _fetch_one_off(
        request, "/api/v1/one-off-customers", search=search, limit=limit, offset=offset,
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "parties/one_off_bucket.html",
        {
            "title": "One-off customers",
            "subtitle": "Once-off invoices, walk-in sales",
            "icon": "user-plus",
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search or "",
            "error": error,
            "base_path": "/one-off-customers",
            "amount_column": "total_billed",
            "amount_label": "Total billed",
        },
    )
