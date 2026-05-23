"""Web-layer proxy for /admin/inspect — fronts the API endpoint of the same
name so the browser can hit it with the session cookie.

Gated triple-redundant: web feature flag check + browser-visible button
guarded by template flag + API gate on the upstream endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from saebooks_web.api_client import api_client
from saebooks_web.features import is_feature_enabled

router = APIRouter()


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


@router.get("/admin/inspect/{table}/{row_id}", response_class=JSONResponse, response_model=None)
async def web_inspect(table: str, row_id: str, request: Request) -> JSONResponse | RedirectResponse:
    """Proxy to /api/v1/admin/inspect — flag-gated."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not is_feature_enabled("raw_json_inspector"):
        return JSONResponse({"error": "not enabled"}, status_code=404)
    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/admin/inspect/{table}/{row_id}")
    try:
        body = resp.json()
    except Exception:
        body = {"error": "non-JSON upstream"}
    return JSONResponse(body, status_code=resp.status_code)
