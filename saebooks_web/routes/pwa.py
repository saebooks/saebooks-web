"""PWA endpoints — manifest + service worker at origin root.

The Web App Manifest can technically be served from any path, but the
service worker MUST be served from the origin root (or a parent of the
scope it claims). We register the SW from `/sw.js` so it controls
everything under `/`.

    GET /sw.js                → static/pwa/sw.js, brand-substituted
    GET /manifest.webmanifest → static/manifest.webmanifest, brand-overridden

Both are rendered per brand rather than served as flat files
--------------------------------------------------------------
These two were the last places the SAE Books name was hardcoded outside a
template, so the 2026-08-11 rebrand did not reach them: `current_brand()` is a
Jinja global and neither file goes through Jinja.

That matters more here than it looks. An installed PWA caches the manifest's
name and icons *at install time*, so a stale manifest leaves the old identity
sitting on the user's home screen until they uninstall and reinstall. The
service worker's offline page is likewise the one screen a user sees when
nothing else is reachable — the wrong product name there is conspicuous.

The static files remain the source of the stable structure (shortcuts,
categories, display mode, the whole SW implementation); only the brand-derived
fields are overridden on the way out.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from saebooks_web.brand import current_brand

router = APIRouter()

# Token the static sw.js carries in place of the product name. Substituted at
# serve time; keeping a placeholder rather than the literal means a future
# brand cannot silently ship the wrong name in the offline page.
_SW_BRAND_TOKEN = "__BRAND_NAME__"


def _static_dir() -> Path:
    """Resolve the static directory — same logic as main.py.

    In Docker the file lives at ``/app/static``. In dev, resolve relative
    to the repo root.
    """
    docker_static = Path("/app/static")
    if docker_static.exists():
        return docker_static
    return Path(__file__).resolve().parent.parent.parent / "static"


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Serve the service worker from origin root so its scope is `/`."""
    path = _static_dir() / "pwa" / "sw.js"
    body = path.read_text(encoding="utf-8").replace(
        _SW_BRAND_TOKEN, current_brand().name
    )
    return Response(
        content=body,
        media_type="application/javascript",
        headers={
            # Service workers must NOT be cached aggressively — the browser
            # already caps their cache; setting no-cache forces a revalidate
            # so we can ship a new SW promptly.
            "Cache-Control": "no-cache",
            # Required so the SW can claim a scope wider than its own path.
            # FastAPI strips this if not set explicitly; nginx/Caddy may also
            # need this header.
            "Service-Worker-Allowed": "/",
        },
    )


def build_manifest() -> dict:
    """Return the web app manifest for the active brand.

    Structural fields (shortcuts, categories, display, scope) come from the
    static file; everything that names or depicts the product comes from the
    brand. Exposed separately from the route so tests can assert the document
    without going through HTTP.
    """
    path = _static_dir() / "manifest.webmanifest"
    manifest: dict = json.loads(path.read_text(encoding="utf-8"))
    brand = current_brand()

    manifest["name"] = brand.name
    manifest["short_name"] = brand.name
    manifest["description"] = brand.pwa_description
    manifest["lang"] = brand.pwa_lang
    manifest["icons"] = [
        {
            "src": brand.pwa_icon_192,
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": brand.pwa_icon_512,
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": brand.pwa_icon_maskable_512,
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    return manifest


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest_webmanifest() -> Response:
    """Serve the web app manifest with the official MIME type."""
    return JSONResponse(
        build_manifest(), media_type="application/manifest+json"
    )


@router.get("/manifest.json", include_in_schema=False)
async def manifest_json() -> Response:
    """Backwards-compatible alias for older user-agents."""
    return JSONResponse(
        build_manifest(), media_type="application/manifest+json"
    )
