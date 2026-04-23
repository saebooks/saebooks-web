"""Thin httpx wrapper that injects the session bearer token on every call.

Usage (inside a request handler)::

    from saebooks_web.api_client import api_client
    from fastapi import Request

    async def some_view(request: Request):
        async with api_client(request) as client:
            resp = await client.get("/api/v1/contacts")
            resp.raise_for_status()
            data = resp.json()

The wrapper reads the bearer token stored in the signed session cookie and
adds ``Authorization: Bearer <token>`` to every outbound request.  Raises
``httpx.HTTPStatusError`` on non-2xx responses (caller decides how to handle).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import Request

from saebooks_web.config import settings


@asynccontextmanager
async def api_client(request: Request) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield an httpx.AsyncClient pre-configured for the saebooks-api.

    The ``Authorization`` header is injected when a bearer token is present
    in the session.  Callers can make requests against ``/api/v1/...`` paths
    and the base URL is automatically prepended.
    """
    token: str | None = request.session.get("api_token")
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(
        base_url=settings.api_url,
        headers=headers,
        timeout=10.0,
    ) as client:
        yield client
