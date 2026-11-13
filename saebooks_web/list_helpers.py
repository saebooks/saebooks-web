"""Shared plumbing for the list screens: search, amount range, page size,
contact names for the rows on a page, and spreadsheet (CSV) downloads.

The engine does the searching, sorting and paging (``q``, ``amount_min``,
``amount_max``, ``sort``, ``direction``, ``format=csv`` on every list
endpoint). These helpers keep each list route small and make every list
behave the same way.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi.responses import Response

# Page sizes offered on every list (P1-46). The engine caps a page at 500.
PAGE_SIZES: tuple[int, ...] = (25, 50, 100, 200, 500)
DEFAULT_PAGE_SIZE = 50


def clamp_page_size(limit: Any) -> int:
    """Snap ``limit`` to the nearest offered page size (default 50)."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if n <= 0:
        return DEFAULT_PAGE_SIZE
    for size in PAGE_SIZES:
        if n <= size:
            return size
    return PAGE_SIZES[-1]


def clean_amount(value: str | None) -> str:
    """Return ``value`` as a plain decimal string, or "" if it is not money.

    Accepts "$1,200.50" style input; anything unreadable is dropped rather
    than sent to the engine as a 422.
    """
    if not value:
        return ""
    text = value.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return ""
    try:
        return str(Decimal(text))
    except InvalidOperation:
        return ""


def search_params(q: str | None, amount_min: str | None, amount_max: str | None) -> dict[str, str]:
    """Engine query params for the shared search inputs (empty ones dropped)."""
    out: dict[str, str] = {}
    q = (q or "").strip()
    if q:
        out["q"] = q[:200]
    lo, hi = clean_amount(amount_min), clean_amount(amount_max)
    if lo:
        out["amount_min"] = lo
    if hi:
        out["amount_max"] = hi
    return out


def wants_csv(export: str | None) -> bool:
    return (export or "").strip().lower() == "csv"


async def fetch_contact_names(
    client: httpx.AsyncClient, ids: Any
) -> dict[str, dict[str, Any]]:
    """``{id: contact}`` for exactly the contacts referenced on a page.

    Replaces the old "fetch the first 500 suppliers and hope" approach,
    which left names blank on big books (P1-46).
    """
    wanted = sorted({str(i) for i in ids if i})
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), 200):
        chunk = wanted[start:start + 200]
        resp = await client.get(
            "/api/v1/contacts",
            params=[("id", cid) for cid in chunk] + [("limit", "500")],
        )
        if resp.is_success:
            for c in resp.json().get("items", []):
                out[str(c["id"])] = c
    return out


async def contact_name(client: httpx.AsyncClient, contact_id: str | None) -> str:
    """Name of one contact (for showing the current filter), or ""."""
    if not contact_id:
        return ""
    names = await fetch_contact_names(client, [contact_id])
    c = names.get(str(contact_id))
    return str(c.get("name") or "") if c else ""


def csv_passthrough(resp: httpx.Response, fallback_name: str) -> Response:
    """Hand the engine's CSV straight to the browser as a download."""
    disposition = resp.headers.get(
        "content-disposition", f'attachment; filename="{fallback_name}"'
    )
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "text/csv; charset=utf-8"),
        headers={"Content-Disposition": disposition},
    )


def export_error_message(resp: httpx.Response) -> str:
    """Plain-words reason an export was refused (e.g. too many rows)."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, str) and detail:
        return detail
    return f"The download could not be made (the books service answered {resp.status_code})."


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "PAGE_SIZES",
    "clamp_page_size",
    "clean_amount",
    "contact_name",
    "csv_passthrough",
    "export_error_message",
    "fetch_contact_names",
    "search_params",
    "wants_csv",
]
