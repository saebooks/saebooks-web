"""Global search route — Lane D cycle 42.

GET /search?q=foo  — calls GET /api/v1/search?q=foo, renders full page
GET /search?q=     — renders page with empty-query prompt (no API call)
GET /search?q=foo  (with HX-Request: true) — returns partial fragment only

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# API url-prefix → saebooks-web url-prefix mapping.
# The UUID slug after the prefix is preserved unchanged.
_PREFIX_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^/contacts/"), "/contacts/"),
    (re.compile(r"^/invoices/"), "/invoices/"),
    (re.compile(r"^/bills/"), "/bills/"),
    (re.compile(r"^/accounts/"), "/accounts/"),
]

# Kind -> singular badge label, plural group heading, badge colour.
# Display order follows the engine's (contacts first, accounts last).
_KIND_META: dict[str, dict[str, str]] = {
    "contact":  {"label": "Contact", "plural": "Contacts", "badge": "bg-blue-100 text-blue-700"},
    "invoice":  {"label": "Invoice", "plural": "Invoices", "badge": "bg-green-100 text-green-700"},
    "bill":     {"label": "Bill", "plural": "Bills", "badge": "bg-amber-100 text-amber-700"},
    "payment":  {"label": "Payment", "plural": "Payments", "badge": "bg-teal-100 text-teal-700"},
    "receipt":  {"label": "Receipt", "plural": "Receipts", "badge": "bg-teal-100 text-teal-700"},
    "credit_note": {"label": "Credit note", "plural": "Credit notes", "badge": "bg-rose-100 text-rose-700"},
    "supplier_credit_note": {"label": "Supplier credit", "plural": "Supplier credits", "badge": "bg-rose-100 text-rose-700"},
    "quote":    {"label": "Quote", "plural": "Quotes", "badge": "bg-indigo-100 text-indigo-700"},
    "purchase_order": {"label": "Purchase order", "plural": "Purchase orders", "badge": "bg-indigo-100 text-indigo-700"},
    "expense":  {"label": "Expense", "plural": "Expenses", "badge": "bg-orange-100 text-orange-700"},
    "account":  {"label": "Account", "plural": "Accounts", "badge": "bg-gray-100 text-gray-700"},
}

# Display order for grouped results.
_KIND_ORDER = list(_KIND_META)

# Hits per kind on the quick results page, and per page on "view all".
_QUICK_LIMIT = 10
_ALL_LIMIT = 50


def _remap_url(api_url: str) -> str:
    """Rewrite an API url field to the saebooks-web route prefix.

    The URL shapes from the API are already ``/contacts/uuid``,
    ``/invoices/uuid``, etc. — the prefix is the same for saebooks-web, so
    this is effectively a no-op for the current mapping.  The function exists
    so that if the API ever changes its prefix scheme we have one place to fix.
    """
    for pattern, replacement in _PREFIX_MAP:
        if pattern.match(api_url):
            return pattern.sub(replacement, api_url, count=1)
    return api_url


def _enrich_hits(hits: list[dict]) -> list[dict]:
    """Add ``web_url``, ``kind_label``, and ``badge_class`` to each hit."""
    enriched = []
    for hit in hits:
        kind = hit.get("kind", "")
        meta = _KIND_META.get(
            kind,
            {"label": kind.replace("_", " ").capitalize(), "badge": "bg-gray-100 text-gray-700"},
        )
        enriched.append({
            **hit,
            "web_url": _remap_url(hit.get("url", "#")),
            "kind_label": meta["label"],
            "badge_class": meta["badge"],
        })
    return enriched


def _group_hits(
    hits: list[dict], counts: dict[str, int] | None = None
) -> list[dict[str, object]]:
    """Group hits into [{kind, kind_label, hits, count}, ...] in display order.

    ``count`` is the engine's full match count for the kind (it can exceed
    the hits shown; the template then offers "view all").
    """
    counts = counts or {}
    by_kind: dict[str, list[dict]] = {}
    for hit in hits:
        by_kind.setdefault(hit["kind"], []).append(hit)

    groups = []
    seen: set[str] = set()
    for kind in _KIND_ORDER:
        if kind in by_kind:
            meta = _KIND_META[kind]
            groups.append({
                "kind": kind, "kind_label": meta["plural"], "hits": by_kind[kind],
                "count": counts.get(kind, len(by_kind[kind])),
            })
            seen.add(kind)

    # Append any unknown kinds not in _KIND_ORDER.
    for kind, kind_hits in by_kind.items():
        if kind not in seen:
            groups.append({
                "kind": kind, "kind_label": kind.replace("_", " ").capitalize(),
                "hits": kind_hits, "count": counts.get(kind, len(kind_hits)),
            })

    return groups


@router.get("/search", response_class=HTMLResponse, response_model=None)
async def search(
    request: Request,
    q: str = "",
    kind: str = "",
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """Render the global search results page (full or HTMX partial).

    - Empty ``q`` → render page with a prompt, no API call.
    - Non-empty ``q`` → up to 10 hits per kind, each kind with its full
      count and a "view all" link when there are more.
    - ``kind=invoice`` (etc.) → every match of that one kind, 50 a page.
    - ``HX-Request: true`` header → return ``search/_results.html`` partial only.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    is_htmx = request.headers.get("HX-Request") == "true"
    kind = kind if kind in _KIND_META else ""
    offset = max(0, offset)

    hits: list[dict] = []
    groups: list[dict] = []
    counts: dict[str, int] = {}
    total: int = 0
    error: str | None = None

    if q.strip():
        params: dict[str, object] = {"q": q.strip()[:200]}
        if kind:
            params.update({"kind": kind, "limit": _ALL_LIMIT, "offset": offset})
        else:
            params["limit"] = _QUICK_LIMIT
        async with api_client(request) as client:
            resp = await client.get("/api/v1/search", params=params)
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                payload = resp.json()
                hits = _enrich_hits(payload.get("hits", []))
                counts = payload.get("counts") or {}
                total = sum(counts.values()) if counts else payload.get("total", len(hits))
                groups = _group_hits(hits, counts)
            else:
                error = f"Search failed: HTTP {resp.status_code}"

    ctx: dict[str, object] = {
        "q": q,
        "kind": kind,
        "kind_meta": _KIND_META,
        "offset": offset,
        "page_limit": _ALL_LIMIT,
        "hits": hits,
        "groups": groups,
        "counts": counts,
        "total": total,
        "error": error,
    }

    template = "search/_results.html" if is_htmx else "search/results.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)
