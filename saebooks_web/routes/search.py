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

# Kind-label and badge-colour used in templates.
_KIND_META: dict[str, dict[str, str]] = {
    "contact":  {"label": "Contact",  "badge": "bg-blue-100 text-blue-700"},
    "invoice":  {"label": "Invoice",  "badge": "bg-green-100 text-green-700"},
    "bill":     {"label": "Bill",     "badge": "bg-amber-100 text-amber-700"},
    "account":  {"label": "Account",  "badge": "bg-gray-100 text-gray-700"},
}

# Display order for grouped results.
_KIND_ORDER = ["contact", "invoice", "bill", "account"]


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
        meta = _KIND_META.get(hit.get("kind", ""), {"label": hit.get("kind", ""), "badge": "bg-gray-100 text-gray-700"})
        enriched.append({
            **hit,
            "web_url": _remap_url(hit.get("url", "#")),
            "kind_label": meta["label"],
            "badge_class": meta["badge"],
        })
    return enriched


def _group_hits(hits: list[dict]) -> list[dict[str, object]]:
    """Group hits into [{kind, kind_label, hits: [...]}, ...] in display order."""
    by_kind: dict[str, list[dict]] = {}
    for hit in hits:
        by_kind.setdefault(hit["kind"], []).append(hit)

    groups = []
    seen: set[str] = set()
    for kind in _KIND_ORDER:
        if kind in by_kind:
            meta = _KIND_META.get(kind, {"label": kind.title(), "badge": ""})
            groups.append({"kind": kind, "kind_label": meta["label"], "hits": by_kind[kind]})
            seen.add(kind)

    # Append any unknown kinds not in _KIND_ORDER.
    for kind, kind_hits in by_kind.items():
        if kind not in seen:
            groups.append({"kind": kind, "kind_label": kind.title(), "hits": kind_hits})

    return groups


@router.get("/search", response_class=HTMLResponse, response_model=None)
async def search(
    request: Request,
    q: str = "",
) -> HTMLResponse | RedirectResponse:
    """Render the global search results page (full or HTMX partial).

    - Empty ``q`` → render page with a prompt, no API call.
    - Non-empty ``q`` → call ``GET /api/v1/search?q=<q>``, render results.
    - ``HX-Request: true`` header → return ``search/_results.html`` partial only.
    """
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)

    is_htmx = request.headers.get("HX-Request") == "true"

    hits: list[dict] = []
    groups: list[dict] = []
    total: int = 0
    error: str | None = None

    if q.strip():
        async with api_client(request) as client:
            resp = await client.get("/api/v1/search", params={"q": q})
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.is_success:
                payload = resp.json()
                hits = _enrich_hits(payload.get("hits", []))
                total = payload.get("total", len(hits))
                groups = _group_hits(hits)
            else:
                error = f"Search failed: HTTP {resp.status_code}"

    ctx: dict[str, object] = {
        "q": q,
        "hits": hits,
        "groups": groups,
        "total": total,
        "error": error,
    }

    template = "search/_results.html" if is_htmx else "search/results.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)
