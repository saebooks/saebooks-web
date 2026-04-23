"""Shared archive helper for all 6 entity types — Lane D cycle 20.

Each entity's archive route calls ``archive_entity`` with the appropriate
paths.  The helper issues ``DELETE /api/v1/{entity_path}/{id}`` with an
``If-Match: <version>`` header and handles the three outcomes:

- 204 No Content  → 303 redirect to *list_url* with session flash "<Label> archived."
- 409 Conflict    → 303 redirect to *detail_url* with conflict flash.
- 422 Unprocessable → 303 redirect to *detail_url* with the API-provided message.
- Other 4xx/5xx   → 303 redirect to *detail_url* with a generic flash.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

from saebooks_web.api_client import api_client


async def archive_entity(
    request: Request,
    entity_api_path: str,
    entity_id: str,
    version: str,
    entity_label: str,
    list_url: str,
    detail_url: str,
) -> RedirectResponse:
    """Issue DELETE with If-Match and redirect appropriately.

    Parameters
    ----------
    entity_api_path:
        The API path segment, e.g. ``"/api/v1/invoices"``.
    entity_id:
        UUID string of the entity to archive.
    version:
        Current version from the hidden form field (used as If-Match value).
    entity_label:
        Human-readable name shown in flash messages, e.g. ``"Invoice INV-0001"``.
    list_url:
        Where to redirect on success, e.g. ``"/invoices"``.
    detail_url:
        Where to redirect on failure, e.g. ``"/invoices/{id}"``.
    """
    async with api_client(request) as client:
        resp = await client.delete(
            f"{entity_api_path}/{entity_id}",
            headers={"If-Match": version},
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 204:
        request.session["flash"] = f"{entity_label} archived."
        return RedirectResponse(url=list_url, status_code=303)

    if resp.status_code == 409:
        request.session["flash"] = (
            "Archive failed — item was modified. Refresh and try again."
        )
        return RedirectResponse(url=detail_url, status_code=303)

    if resp.status_code == 422:
        # Try to extract the API-provided reason.
        flash_msg = "Archive failed."
        try:
            detail = resp.json().get("detail", "")
            if isinstance(detail, str) and detail:
                flash_msg = detail
            elif isinstance(detail, list) and detail:
                flash_msg = detail[0].get("msg", flash_msg)
        except Exception:
            pass
        request.session["flash"] = flash_msg
        return RedirectResponse(url=detail_url, status_code=303)

    # Unexpected error (500, etc.)
    request.session["flash"] = (
        f"Archive failed — unexpected error (HTTP {resp.status_code})."
    )
    return RedirectResponse(url=detail_url, status_code=303)
