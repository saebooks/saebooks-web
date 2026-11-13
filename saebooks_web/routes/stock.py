"""Stock screens (batch 12: P1-65, P1-66, P1-68).

GET  /stock                          — stock on hand and value per item,
                                       tied to the general ledger; CSV
GET  /stock/adjustments              — stocktakes and write-offs
GET  /stock/adjustments/new          — new adjustment (?item_id= prefill)
POST /stock/adjustments/new          — save as draft
GET  /stock/adjustments/{id}         — detail
POST /stock/adjustments/{id}/post    — post (moves stock, posts journal)
POST /stock/adjustments/{id}/void    — void
POST /stock/adjustments/{id}/delete  — delete a draft
GET  /stock/reports/sales-by-item    — sales, cost and margin per item; CSV
GET  /stock/reports/purchases-by-item
GET  /reports/purchases-by-supplier  — spend per supplier; CSV

Every call goes through the engine API; nothing is computed here.
"""
from __future__ import annotations

import contextlib
import csv
import io
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_REASONS = (
    ("stocktake", "Stocktake count"),
    ("damaged", "Damaged"),
    ("lost", "Lost or stolen"),
    ("found", "Found"),
    ("own_use", "Taken for own use"),
    ("other", "Other"),
)


def _auth(request: Request) -> bool:
    return bool(request.session.get("api_token"))


def _detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        return "; ".join(str(d.get("msg", d)) for d in detail)
    return str(detail or f"HTTP {resp.status_code}")


def _csv(filename: str, header: list[str], rows: list[list[Any]]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Valuation                                                                   #
# --------------------------------------------------------------------------- #


@router.get("/stock", response_class=HTMLResponse, response_model=None)
async def stock_valuation(
    request: Request, as_of: str | None = None, format: str | None = None
) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    params = {"as_of": as_of} if as_of else {}
    report: dict[str, Any] | None = None
    error = None
    async with api_client(request) as client:
        resp = await client.get("/api/v1/stock/valuation", params=params)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        report = resp.json()
    elif resp.status_code == 404:
        error = "Stock needs the inventory module, which is not turned on for this company."
    else:
        error = f"Could not load the stock report ({_detail(resp)})."
    if format == "csv" and report:
        return _csv(
            f"stock-value-{report['as_of']}.csv",
            ["Code", "Item", "Unit", "Quantity", "Average cost", "Value", "Low on stock"],
            [
                [r["sku"], r["name"], r.get("unit_of_measure") or "", r["quantity"],
                 r["average_cost"], r["value"], "yes" if r.get("below_reorder_level") else ""]
                for r in report["rows"]
            ],
        )
    return _TEMPLATES.TemplateResponse(
        request,
        "stock/valuation.html",
        {"report": report, "error": error, "as_of": as_of or ""},
    )


# --------------------------------------------------------------------------- #
# Adjustments                                                                 #
# --------------------------------------------------------------------------- #


async def _adjustment_refs(request: Request) -> dict[str, Any]:
    items: list[dict] = []
    accounts: list[dict] = []
    async with api_client(request) as client:
        r = await client.get(
            "/api/v1/items", params={"item_type": "inventory", "limit": 1000}
        )
        if r.is_success:
            items = r.json().get("items", [])
        a = await client.get("/api/v1/accounts", params={"limit": 500, "offset": 0})
        if a.is_success:
            accounts = a.json().get("items", [])
    return {"items": items, "accounts": accounts, "reasons": _REASONS}


@router.get("/stock/adjustments", response_class=HTMLResponse, response_model=None)
async def adjustments_list(request: Request, status: str | None = None) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    params = {"status": status} if status else {}
    async with api_client(request) as client:
        resp = await client.get("/api/v1/stock/adjustments", params=params)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    rows = resp.json().get("items", []) if resp.is_success else []
    error = None if resp.is_success else f"Could not load adjustments ({_detail(resp)})."
    return _TEMPLATES.TemplateResponse(
        request,
        "stock/adjustments_list.html",
        {
            "adjustments": rows,
            "error": error,
            "status": status or "",
            "flash": request.session.pop("flash", None),
        },
    )


@router.get("/stock/adjustments/new", response_class=HTMLResponse, response_model=None)
async def adjustment_new(request: Request, item_id: str | None = None) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    refs = await _adjustment_refs(request)
    lines = [{"item_id": item_id or "", "counted_qty": "", "quantity_change": "", "unit_cost": ""}]
    lines += [{"item_id": "", "counted_qty": "", "quantity_change": "", "unit_cost": ""} for _ in range(4)]
    return _TEMPLATES.TemplateResponse(
        request,
        "stock/adjustment_new.html",
        {
            **refs,
            "form": {"adjustment_date": date.today().isoformat(), "reason": "stocktake"},
            "lines": lines,
            "error": None,
        },
    )


def _parse_adjustment_form(form: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    idx: set[int] = set()
    for key in form:
        if key.startswith("lines[") and "][" in key:
            with contextlib.suppress(ValueError):
                idx.add(int(key.split("[")[1].split("]")[0]))
    raw_lines = []
    api_lines = []
    for i in sorted(idx):
        row = {
            f: (form.get(f"lines[{i}][{f}]") or "").strip()
            for f in ("item_id", "counted_qty", "quantity_change", "unit_cost", "description")
        }
        raw_lines.append(row)
        if not row["item_id"]:
            continue
        line: dict[str, Any] = {"item_id": row["item_id"]}
        for f in ("counted_qty", "quantity_change", "unit_cost", "description"):
            if row[f]:
                line[f] = row[f]
        api_lines.append(line)
    payload = {
        "adjustment_date": (form.get("adjustment_date") or "").strip(),
        "account_id": (form.get("account_id") or "").strip(),
        "reason": (form.get("reason") or "stocktake").strip(),
        "notes": (form.get("notes") or "").strip() or None,
        "lines": api_lines,
    }
    return payload, raw_lines


@router.post("/stock/adjustments/new", response_class=HTMLResponse, response_model=None)
async def adjustment_create(request: Request) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = {k: str(v) for k, v in (await request.form()).items()}
    payload, raw_lines = _parse_adjustment_form(form)
    error = None
    if not payload["lines"]:
        error = "Add at least one item."
    elif not payload["account_id"]:
        error = "Choose the account for the other side of the adjustment."
    if error is None:
        async with api_client(request) as client:
            resp = await client.post("/api/v1/stock/adjustments", json=payload)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 201:
            adj = resp.json()
            request.session["flash"] = "Adjustment saved as a draft. Check it, then post it."
            return RedirectResponse(url=f"/stock/adjustments/{adj['id']}", status_code=303)
        error = _detail(resp)
    refs = await _adjustment_refs(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "stock/adjustment_new.html",
        {**refs, "form": form, "lines": raw_lines or [{}], "error": error},
        status_code=422,
    )


@router.get("/stock/adjustments/{adjustment_id}", response_class=HTMLResponse, response_model=None)
async def adjustment_detail(request: Request, adjustment_id: str) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/stock/adjustments/{adjustment_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        refs = await _adjustment_refs(request)
    adj = resp.json() if resp.is_success else None
    items_by_id = {i["id"]: i for i in refs["items"]}
    accounts_by_id = {a["id"]: a for a in refs["accounts"]}
    return _TEMPLATES.TemplateResponse(
        request,
        "stock/adjustment_detail.html",
        {
            "adj": adj,
            "items_by_id": items_by_id,
            "accounts_by_id": accounts_by_id,
            "reasons": dict(_REASONS),
            "error": None if adj else "Stock adjustment not found.",
            "flash": request.session.pop("flash", None),
        },
        status_code=200 if adj else 404,
    )


async def _transition(request: Request, adjustment_id: str, action: str) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    version = str(form.get("version", ""))
    async with api_client(request) as client:
        if action == "delete":
            resp = await client.delete(f"/api/v1/stock/adjustments/{adjustment_id}")
        else:
            resp = await client.post(
                f"/api/v1/stock/adjustments/{adjustment_id}/{action}",
                headers={"If-Match": version},
            )
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if action == "delete" and resp.is_success:
        request.session["flash"] = "Draft adjustment deleted."
        return RedirectResponse(url="/stock/adjustments", status_code=303)
    if resp.is_success:
        request.session["flash"] = (
            "Adjustment posted. Stock and the ledger are updated."
            if action == "post"
            else "Adjustment voided. Stock and the ledger are put back."
        )
    elif resp.status_code == 409:
        request.session["flash"] = "Someone changed this adjustment. Reload and try again."
    else:
        request.session["flash"] = f"Could not {action} the adjustment: {_detail(resp)}"
    return RedirectResponse(url=f"/stock/adjustments/{adjustment_id}", status_code=303)


@router.post("/stock/adjustments/{adjustment_id}/post", response_model=None)
async def adjustment_post(request: Request, adjustment_id: str) -> Response:
    return await _transition(request, adjustment_id, "post")


@router.post("/stock/adjustments/{adjustment_id}/void", response_model=None)
async def adjustment_void(request: Request, adjustment_id: str) -> Response:
    return await _transition(request, adjustment_id, "void")


@router.post("/stock/adjustments/{adjustment_id}/delete", response_model=None)
async def adjustment_delete(request: Request, adjustment_id: str) -> Response:
    return await _transition(request, adjustment_id, "delete")


# --------------------------------------------------------------------------- #
# Item and supplier reports                                                   #
# --------------------------------------------------------------------------- #

_REPORTS: dict[str, dict[str, Any]] = {
    "sales-by-item": {
        "api": "/api/v1/stock/reports/sales-by-item",
        "title": "Sales by item",
        "intro": "What sold, for how much, what it cost and the margin. "
                 "Credit notes for an item are taken off. Amounts exclude GST.",
        "columns": [
            ("sku", "Code", "text"), ("name", "Item", "text"),
            ("quantity", "Quantity", "qty"), ("sales", "Sales", "money"),
            ("cost_of_sales", "Cost of sales", "money"), ("margin", "Margin", "money"),
            ("margin_percent", "Margin %", "pct"),
        ],
        "totals": ["quantity", "sales", "cost_of_sales", "margin"],
    },
    "purchases-by-item": {
        "api": "/api/v1/stock/reports/purchases-by-item",
        "title": "Purchases by item",
        "intro": "What was bought on posted bills and the average cost. Amounts exclude GST.",
        "columns": [
            ("sku", "Code", "text"), ("name", "Item", "text"),
            ("quantity", "Quantity", "qty"), ("cost", "Cost", "money"),
            ("average_cost", "Average cost", "money"),
        ],
        "totals": ["quantity", "cost"],
    },
    "purchases-by-supplier": {
        "api": "/api/v1/reports/purchases-by-supplier",
        "title": "Purchases by supplier",
        "intro": "Spend per supplier on posted bills and expenses.",
        "columns": [
            ("name", "Supplier", "text"), ("documents", "Documents", "qty"),
            ("bills_total", "Bills", "money"), ("expenses_total", "Expenses", "money"),
            ("subtotal", "Excluding GST", "money"), ("tax", "GST", "money"),
            ("total", "Total", "money"),
        ],
        "totals": ["bills_total", "expenses_total", "subtotal", "tax", "total"],
    },
}


async def _report(
    request: Request, key: str, date_from: str | None, date_to: str | None, fmt: str | None
) -> Response:
    if not _auth(request):
        return RedirectResponse(url="/login", status_code=303)
    spec = _REPORTS[key]
    today = date.today()
    fy_start = date(today.year if today.month >= 7 else today.year - 1, 7, 1)
    date_from = date_from if date_from is not None else fy_start.isoformat()
    date_to = date_to if date_to is not None else today.isoformat()
    params = {k: v for k, v in (("date_from", date_from), ("date_to", date_to)) if v}
    async with api_client(request) as client:
        resp = await client.get(spec["api"], params=params)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    data = resp.json() if resp.is_success else None
    error = None
    if not resp.is_success:
        error = (
            "This report needs the inventory module."
            if resp.status_code == 404
            else f"Could not load the report ({_detail(resp)})."
        )
    if fmt == "csv" and data:
        return _csv(
            f"{key}-{date_from or 'all'}-{date_to or 'all'}.csv",
            [c[1] for c in spec["columns"]],
            [[r.get(c[0]) if r.get(c[0]) is not None else "" for c in spec["columns"]]
             for r in data["rows"]],
        )
    return _TEMPLATES.TemplateResponse(
        request,
        "stock/report.html",
        {
            "spec": spec, "key": key, "data": data, "error": error,
            "date_from": date_from, "date_to": date_to,
        },
    )


@router.get("/stock/reports/sales-by-item", response_class=HTMLResponse, response_model=None)
async def sales_by_item(
    request: Request, date_from: str | None = None, date_to: str | None = None,
    format: str | None = None,
) -> Response:
    return await _report(request, "sales-by-item", date_from, date_to, format)


@router.get("/stock/reports/purchases-by-item", response_class=HTMLResponse, response_model=None)
async def purchases_by_item(
    request: Request, date_from: str | None = None, date_to: str | None = None,
    format: str | None = None,
) -> Response:
    return await _report(request, "purchases-by-item", date_from, date_to, format)


@router.get("/reports/purchases-by-supplier", response_class=HTMLResponse, response_model=None)
async def purchases_by_supplier(
    request: Request, date_from: str | None = None, date_to: str | None = None,
    format: str | None = None,
) -> Response:
    return await _report(request, "purchases-by-supplier", date_from, date_to, format)
