"""Deployment-level brand config for the web layer.

``SAEBOOKS_BRAND`` picks the active brand at process level — ``saebooks``
(default) or ``tasur``. Read directly from ``os.environ`` at call time, the
same lightweight convention ``features.py`` uses for ``SAEBOOKS_EDITION``
(no ``SAEBOOKS_WEB_`` prefix, no pydantic Settings coupling — this is a
deployment-level concept, not a per-process web tunable).

``current_brand()`` is registered as a Jinja global via the existing
``Jinja2Templates.__init__`` patch in ``saebooks_web/security/__init__.py``
— the same injection hook that already delivers ``is_feature_enabled`` /
``current_edition`` (``register_feature_global``). This is the single global
hook for all 61 template envs; no route module needs touching.

Templates call it exactly like ``current_edition()``:

    {% set brand = current_brand() %}
    <title>{{ brand.name }}</title>

Per the EE GUI prep scope (decision 3): brand text is interpolated *inside*
translatable strings via a plain Jinja variable, never post-hoc swapped in
rendered HTML. That means it is already MT-safe for the future gettext pass
(P2) — Babel extracts ``{{ brand.name }}`` as a placeholder, never as
literal text to translate, so no rework is needed when strings are wrapped
in ``{% trans %}``. This packet (P1) does not wrap any strings — it only
swaps the previously-hardcoded "SAE Books" literal for this variable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    application_name: str
    meta_description: str
    wordmark_src: str
    wordmark_alt: str
    favicon_32: str
    favicon_16: str
    apple_touch_icon: str


_BRANDS: dict[str, Brand] = {
    "saebooks": Brand(
        key="saebooks",
        name="SAE Books",
        application_name="SAE Books",
        meta_description="API-first accounting for Australian small business.",
        wordmark_src="/static/sae-books-logo.png",
        wordmark_alt="SAE Books",
        favicon_32="/static/pwa/icons/favicon-32.png",
        favicon_16="/static/pwa/icons/favicon-16.png",
        apple_touch_icon="/static/pwa/icons/apple-touch-icon-180.png",
    ),
    "tasur": Brand(
        key="tasur",
        name="Tasur",
        application_name="Tasur",
        meta_description="API-first accounting for Estonian small business.",
        wordmark_src="/static/brand/tasur-wordmark.png",
        wordmark_alt="Tasur",
        # tasur-site ships one 64x64 favicon (no separate 32/16 renditions);
        # reuse it for both <link> sizes — browsers scale down fine.
        favicon_32="/static/brand/tasur-favicon.png",
        favicon_16="/static/brand/tasur-favicon.png",
        apple_touch_icon="/static/brand/tasur-apple-touch-icon.png",
    ),
}

_DEFAULT_BRAND_KEY = "saebooks"


def _current_brand_key() -> str:
    return os.environ.get("SAEBOOKS_BRAND", _DEFAULT_BRAND_KEY).strip().lower() or _DEFAULT_BRAND_KEY


def current_brand() -> Brand:
    """Return the active Brand config for SAEBOOKS_BRAND (defaults to saebooks).

    Unknown values fall back to the saebooks default rather than raising —
    a typo'd env var should degrade to the known-good brand, not 500 every
    page.
    """
    return _BRANDS.get(_current_brand_key(), _BRANDS[_DEFAULT_BRAND_KEY])


def register_brand_global(templates) -> None:
    """Add the ``current_brand`` Jinja global to a Jinja2Templates env.

    Called from the patched ``Jinja2Templates.__init__`` (see
    ``saebooks_web/security/__init__.py``) so every templates instance gets
    it automatically — mirrors ``register_feature_global``'s wiring exactly.
    Registered as the callable (not a pre-resolved value) so it re-reads the
    env var per render, same as ``current_edition``; this matters for tests
    that monkeypatch ``SAEBOOKS_BRAND`` per-request against a templates env
    that was constructed once at module import time.
    """
    try:
        templates.env.globals.setdefault("current_brand", current_brand)
    except AttributeError:
        pass
