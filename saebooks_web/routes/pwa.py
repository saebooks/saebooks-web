"""PWA endpoints — manifest + service worker at origin root.

The Web App Manifest can technically be served from any path, but the
service worker MUST be served from the origin root (or a parent of the
scope it claims). We register the SW from `/sw.js` so it controls
everything under `/`.

Both files live as static assets under `static/`. We expose them at the
root URLs the browser expects:

    GET /sw.js               → static/pwa/sw.js
    GET /manifest.webmanifest → static/manifest.webmanifest

A bare `/manifest.json` is also exposed for older user-agents.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

router = APIRouter()


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
    return FileResponse(
        path,
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


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest_webmanifest() -> Response:
    """Serve the web app manifest with the official MIME type."""
    path = _static_dir() / "manifest.webmanifest"
    return FileResponse(path, media_type="application/manifest+json")


@router.get("/manifest.json", include_in_schema=False)
async def manifest_json() -> Response:
    """Backwards-compatible alias for older user-agents."""
    path = _static_dir() / "manifest.webmanifest"
    return FileResponse(path, media_type="application/manifest+json")
