"""JurisdictionGateMiddleware — page-level defence for AU-only surfaces.

The nav already HIDES the AU-only payroll/BAS pages for a non-AU company
(base.html gates on request.state.jp.features). This is the belt to that
braces: a direct URL (bookmark, typed, stale link) to an AU-only page must not
render Australian content for an EE company either. Redirects to `/` when the
active jurisdiction's presentation contract does not declare the feature the
path needs.

Deliberately conservative: only redirects when the jurisdiction is DEFINITELY
resolved (active_company_jurisdiction set) AND the feature is off. If the
jurisdiction is unresolved (None — logged-out, or a transient presentation
miss), it lets the request through, so an AU user is never wrongly blocked by a
degraded lookup — the nav's cosmetic hiding is the only effect there.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

# path prefix -> the presentation feature that must be on for this jurisdiction
_GATED: tuple[tuple[str, str], ...] = (
    ("/employees", "payroll"),
    ("/pay-run", "payroll"),
    ("/super-funds", "payroll"),
    ("/reports/bas-summary", "tax_reports"),
    ("/reports/bas-payg", "tax_reports"),
    ("/gst", "tax_reports"),
    ("/admin/ato-sbr", "tax_reports"),
)


class JurisdictionGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        for prefix, feature in _GATED:
            if path == prefix or path.startswith(prefix + "/"):
                juris = getattr(request.state, "active_company_jurisdiction", None)
                jp = getattr(request.state, "jp", None)
                # Only block a RESOLVED jurisdiction that genuinely lacks the
                # feature — never an unresolved/degraded one.
                if juris and jp is not None and not getattr(jp.features, feature, False):
                    if "session" in request.scope:
                        request.session["flash"] = (
                            "That section isn't available for this company's jurisdiction."
                        )
                    return RedirectResponse(url="/", status_code=303)
                break
        return await call_next(request)
