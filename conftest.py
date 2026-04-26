"""Repo-root conftest.py — pytest-wide test fixtures and env setup.

Sets SAEBOOKS_WEB_SITE_ORIGIN to ``http://test`` so the OriginRefererMiddleware
treats requests from the AsyncClient ``base_url="http://test"`` as same-origin.
This lets the existing ~270 write-path tests continue to send POST/PUT/PATCH/
DELETE without explicitly forging an Origin header on every call.

In production the env var defaults to https://books-dev.sauer.com.au, so this
override only affects the test process.
"""
from __future__ import annotations

import os

# Set BEFORE any saebooks_web modules are imported (this conftest runs first).
os.environ.setdefault("SAEBOOKS_WEB_SITE_ORIGIN", "http://test")
