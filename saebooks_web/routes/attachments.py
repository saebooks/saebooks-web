"""Attachments panel routes — Phase 1.5 of the saebooks-vault integration.

Three handlers that proxy to the saebooks-api attachments surface, then
re-render the ``_partials/attachments.html`` panel for HTMX outerHTML swaps.

POST /attachments                           — upload, re-render panel
DELETE /attachments/{file_id}               — soft-delete, re-render panel
GET  /attachments/{file_id}/download        — stream blob to client

The saebooks-api itself enforces auth (JWT), tenant scoping, entity ownership,
and vault availability. All this layer does is:

1. Forward the request (with the user's session bearer token via api_client).
2. On 503 — treat vault as disabled, render a graceful disabled panel.
3. On success — re-fetch the attachment list and re-render the full panel so
   HTMX swaps the whole ``#attachments-<kind>-<id>`` div atomically.

Route ordering note: ``/attachments/{file_id}/download`` MUST appear before
``/attachments/{file_id}`` (which is not implemented here but may be added
later) so FastAPI matches the literal sub-path first.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

logger = logging.getLogger("saebooks_web.routes.attachments")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _require_auth(request: Request) -> str | None:
    """Return the bearer token if present, else None."""
    return request.session.get("api_token")


async def _fetch_panel_data(
    request: Request,
    entity_kind: str,
    entity_id: str,
) -> tuple[list[dict], bool]:
    """Fetch the attachment list for the given entity.

    Returns ``(attachments, vault_enabled)``.  A 503 from the API means the
    vault is not configured — we surface the disabled card rather than an
    error banner.  Any other non-200 is logged and treated as an empty list
    (the panel is still rendered so the user can see an upload form even if
    the list fetch glitched).
    """
    async with api_client(request) as client:
        resp = await client.get(
            "/api/v1/attachments",
            params={"entity_kind": entity_kind, "entity_id": entity_id},
        )

    if resp.status_code == 503:
        return [], False
    if resp.is_success:
        return resp.json(), True

    logger.warning(
        "attachment list fetch failed (kind=%s id=%s status=%s)",
        entity_kind, entity_id, resp.status_code,
    )
    return [], True  # vault is up; list just failed


def _render_panel(
    request: Request,
    entity_kind: str,
    entity_id: str,
    attachments: list[dict],
    vault_enabled: bool,
    upload_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "_partials/attachments.html",
        {
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "attachments": attachments,
            "vault_enabled": vault_enabled,
            "upload_error": upload_error,
        },
        status_code=status_code,
    )


@router.post("/attachments", response_class=HTMLResponse, response_model=None)
async def attachment_upload(
    request: Request,
    entity_kind: str = Form(...),
    entity_id: str = Form(...),
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency-injection idiom
) -> HTMLResponse | RedirectResponse:
    """Receive a file upload, forward to saebooks-api, re-render the panel.

    On 503 (vault disabled) — render disabled card.
    On API error — render panel with an upload_error banner.
    On success — re-fetch the attachment list and render the refreshed panel.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    payload = await file.read()
    if not payload:
        attachments, vault_enabled = await _fetch_panel_data(request, entity_kind, entity_id)
        return _render_panel(
            request, entity_kind, entity_id, attachments, vault_enabled,
            upload_error="The selected file is empty.",
        )

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/attachments",
            data={"entity_kind": entity_kind, "entity_id": entity_id},
            files={"file": (file.filename or "upload", payload, file.content_type or "application/octet-stream")},
        )

    if resp.status_code == 503:
        return _render_panel(
            request, entity_kind, entity_id, [], False,
        )

    if not resp.is_success:
        # Try to surface a readable error from the API response.
        try:
            detail = resp.json().get("detail", f"Upload failed (HTTP {resp.status_code})")
            if isinstance(detail, list) and detail:
                detail = detail[0].get("msg", str(detail))
        except Exception:
            detail = f"Upload failed (HTTP {resp.status_code})"
        logger.warning("attachment upload failed: %s %s", resp.status_code, detail)

        attachments, vault_enabled = await _fetch_panel_data(request, entity_kind, entity_id)
        return _render_panel(
            request, entity_kind, entity_id, attachments, vault_enabled,
            upload_error=str(detail),
        )

    # Success — re-fetch the list and re-render the whole panel so the new
    # file appears immediately.
    attachments, vault_enabled = await _fetch_panel_data(request, entity_kind, entity_id)
    return _render_panel(request, entity_kind, entity_id, attachments, vault_enabled)


@router.delete(
    "/attachments/{file_id}",
    response_class=HTMLResponse,
    response_model=None,
)
async def attachment_delete(
    request: Request,
    file_id: str,
    entity_kind: str,
    entity_id: str,
) -> HTMLResponse | RedirectResponse:
    """Soft-delete an attachment and re-render the panel.

    ``entity_kind`` and ``entity_id`` are passed as query params (HTMX sends
    them in the URL, e.g. ``DELETE /attachments/<id>?entity_kind=...&entity_id=...``).
    They are used purely to re-render the panel after deletion — the
    saebooks-api does not require them for the delete itself (it uses the
    file's own tenant-scoped record).
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/attachments/{file_id}")

    if resp.status_code == 503:
        return _render_panel(request, entity_kind, entity_id, [], False)

    if resp.status_code not in (200, 204):
        logger.warning("attachment delete failed: %s", resp.status_code)
        # Re-render with the current list; don't blow up.

    attachments, vault_enabled = await _fetch_panel_data(request, entity_kind, entity_id)
    return _render_panel(request, entity_kind, entity_id, attachments, vault_enabled)


@router.get(
    "/attachments/{file_id}/download",
    response_model=None,
)
async def attachment_download(
    request: Request,
    file_id: str,
) -> StreamingResponse | RedirectResponse:
    """Stream the attachment blob from the saebooks-api to the browser.

    We forward the Content-Disposition and Content-Type headers from the
    upstream response so the browser presents a proper "Save As" dialog
    with the original filename.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # Hit the API download endpoint and relay the streaming response.
    # We open the client inside the generator so the connection stays
    # alive while chunks are being relayed.
    async def _relay():
        async with (
            api_client(request) as client,
            client.stream("GET", f"/api/v1/attachments/{file_id}/download") as api_resp,
        ):
            if not api_resp.is_success:
                logger.warning("download relay: upstream %s for file %s", api_resp.status_code, file_id)
                return
            async for chunk in api_resp.aiter_bytes(chunk_size=65536):
                yield chunk

    # We need the filename + mime before starting the stream so we can
    # set headers. Fetch metadata first (one extra round-trip, but small).
    async with api_client(request) as client:
        meta_resp = await client.get(f"/api/v1/attachments/{file_id}")

    if meta_resp.status_code == 503:
        return RedirectResponse(url="/", status_code=303)
    if not meta_resp.is_success:
        return RedirectResponse(url="/", status_code=303)

    meta = meta_resp.json()
    mime = meta.get("content_type") or "application/octet-stream"
    filename = meta.get("filename") or "download"

    return StreamingResponse(
        _relay(),
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
