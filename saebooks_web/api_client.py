"""Thin httpx wrapper that injects the session bearer token on every call.

Usage (inside a request handler)::

    from saebooks_web.api_client import api_client
    from fastapi import Request

    async def some_view(request: Request):
        async with api_client(request) as client:
            resp = await client.get("/api/v1/contacts")
            resp.raise_for_status()
            data = resp.json()

        # PATCH with optimistic locking:
        async with api_client(request) as client:
            resp = await client.patch(
                "/api/v1/contacts/123",
                json=payload,
                headers={"If-Match": str(version)},
            )

The wrapper reads the bearer token stored in the signed session cookie and
adds ``Authorization: Bearer <token>`` to every outbound request.

Note: httpx.AsyncClient already exposes .get(), .post(), .patch(), .put(),
.delete() etc. — this context manager simply pre-configures the base URL and
auth header.  The .patch() method is available on the yielded client with the
same signature as .post(): ``client.patch(url, *, json=..., headers=..., ...)``.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import Request

from saebooks_web.config import settings


@asynccontextmanager
async def api_client(request: Request) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield an httpx.AsyncClient pre-configured for the saebooks-api.

    The ``Authorization`` header is injected when a bearer token is present
    in the session.  Callers can make requests against ``/api/v1/...`` paths
    and the base URL is automatically prepended.

    The yielded client supports all standard httpx methods including
    ``.patch(url, *, json=..., headers=...)`` for PATCH requests with
    ``If-Match`` optimistic-locking headers.
    """
    token: str | None = request.session.get("api_token")
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # FLAG_TENANT_SWITCHER — when an override is set in session, forward it
    # to the API as X-Active-Tenant. API gate verifies admin + flag before
    # honouring it (see resolve_tenant_id).
    override = request.session.get("active_tenant_override")
    if override:
        headers["X-Active-Tenant"] = str(override)
    # Multi-company switcher (2026-05-24): when the user has picked a
    # company in the header dropdown, inject it so the API resolves to
    # the right company instead of falling back to the first active one.
    active_company_id = request.session.get("active_company_id")
    if active_company_id:
        headers["X-Company-Id"] = str(active_company_id)

    async with httpx.AsyncClient(
        base_url=settings.api_url,
        headers=headers,
        timeout=10.0,
    ) as client:
        yield client
