"""Deployment-level brand config for the web layer.

``SAEBOOKS_BRAND`` picks the active brand at process level — ``tasur``
(default), ``tasur-ee`` or ``saebooks``. Read directly from ``os.environ`` at
call time, the same lightweight convention ``features.py`` uses for
``SAEBOOKS_EDITION`` (no ``SAEBOOKS_WEB_`` prefix, no pydantic Settings
coupling — this is a deployment-level concept, not a per-process web tunable).

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
in ``{% trans %}``.

Brand is presentation; ``market`` is behaviour
----------------------------------------------
Until 2026-08-11 the ``tasur`` key carried *both* the Selge identity and every
Estonian behaviour — eID sign-in, the et/en/ru locale switcher, the EE
onboarding block, Estonian demo copy. That conflation was safe only while
``saebooks`` was the default and ``tasur`` meant "the Estonian deployment".

The 2026-08-11 rebrand makes Tasur the *global product brand*, so the two axes
had to come apart:

* ``tasur``    — the neutral global product brand. No market. English copy,
                 no eID, no locale switcher, jurisdiction-agnostic wording.
* ``tasur-ee`` — the Estonian market surface. Same Selge visuals, Estonian
                 copy, eID, locale switcher.
* ``saebooks`` — retained, unchanged, market ``AU``. Never delete it: pinning
                 ``SAEBOOKS_BRAND=saebooks`` is the one-env-var rollback for
                 any deployment.

Market-conditional behaviour must therefore test ``brand.market``, never the
brand key, and never the ``!= "saebooks"`` negative that two templates used —
that inverted form silently turns EE affordances *on* for every non-AU brand,
including the neutral global default.
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
    # Market this deployment serves — the axis that gates jurisdiction
    # behaviour (eID, locale switcher, EE onboarding, statutory copy).
    # ``None`` = the neutral global surface, which must show none of it.
    # Deliberately separate from ``key`` so a market can be added without
    # minting a brand, and a brand without implying a market.
    market: str | None = None
    # Dark-theme wordmark. The Selge (Tasur) identity ships two theme-scoped
    # renditions — navy ink for light surfaces, pale ink for dark — because a
    # single flat file cannot serve both. ``None`` means the brand has one
    # wordmark for both themes and base.html renders a single ``<img>``.
    wordmark_src_dark: str | None = None
    # Vector favicon, served alongside the PNG renditions where the brand has
    # one. ``None`` = PNG favicons only.
    favicon_svg: str | None = None
    # Dark-theme primary accent (``--sae``) override. The default ramp is SAE
    # Books' own; a brand whose identity fixes a different dark accent sets it
    # here rather than forking the whole variable block. ``None`` = inherit.
    # Light-theme accent needs no field: the shared ``--sae`` light value is
    # already #194291, which is the Selge navy.
    accent_dark: str | None = None
    # Copy for the ephemeral-demo Turnstile gate page (see
    # ``saebooks_web/security/demo_autologin.py``). Kept on the brand so a
    # deployment shows the right copy purely by setting ``SAEBOOKS_BRAND`` —
    # no code edit at deploy time. ``demo_features`` is a short bullet list
    # rendered as ticked ``<li>`` items on the gate.
    demo_tagline: str = "Australian small-business accounting"
    demo_features: tuple[str, ...] = (
        "Full double-entry ledger",
        "Invoices, bills & payments",
        "GST & BAS ready",
        "Bank reconciliation",
    )
    # --- Web App Manifest -------------------------------------------------
    # The manifest is served dynamically by routes/pwa.py rather than as a
    # flat file, because an installed PWA caches name and icons at install
    # time: shipping the wrong brand here means a home-screen icon that keeps
    # the old identity until the user reinstalls the app.
    # ``pwa_description`` is separate from ``meta_description`` because the
    # manifest one is longer and names features, so it cannot be reused.
    pwa_description: str = (
        "API-first accounting for Australian small business — invoices, bills, "
        "BAS, STP. Self-hosted or hosted."
    )
    # BCP-47 tag for the manifest's single ``lang`` field. Per-request UI
    # locale is negotiated separately by LocaleMiddleware; this is only the
    # installed-app default.
    pwa_lang: str = "en-AU"
    pwa_icon_192: str = "/static/pwa/icons/icon-192.png"
    pwa_icon_512: str = "/static/pwa/icons/icon-512.png"
    pwa_icon_maskable_512: str = "/static/pwa/icons/icon-maskable-512.png"


# Selge (Tasur) identity, shared by every Tasur-branded surface regardless of
# market. Direction A brand round 2026-07-21, #194291 navy ramp. Pure-path SVG
# wordmarks, theme-scoped: light surfaces get the #194291 ink rendition, dark
# surfaces the #DCE6FB one. Assets are copied from tasur-site/assets/img/brand,
# which is the source of truth — nothing syncs automatically.
_SELGE_ASSETS: dict[str, str | None] = {
    "wordmark_src": "/static/brand/tasur-wordmark-light.svg",
    "wordmark_src_dark": "/static/brand/tasur-wordmark-dark.svg",
    "wordmark_alt": "Tasur",
    # tasur-site ships one 64x64 favicon (no separate 32/16 renditions);
    # reuse it for both <link> sizes — browsers scale down fine.
    "favicon_32": "/static/brand/tasur-favicon.png",
    "favicon_16": "/static/brand/tasur-favicon.png",
    "favicon_svg": "/static/brand/tasur-icon.svg",
    "apple_touch_icon": "/static/brand/tasur-apple-touch-icon.png",
    # Selge navy-300 — the round's dark-theme accent (7.0:1 on the dark
    # background). #194291 never carries text on dark, so the light-theme
    # value can't simply be reused.
    "accent_dark": "#7D9EE8",
    # PWA renditions derived from the source-of-truth icon-512.png:
    # 192 is a straight downscale; the maskable one is flattened onto opaque
    # #194291 (a maskable icon must have no transparent corners) with the
    # artwork inset to the inner 80% safe zone.
    "pwa_icon_192": "/static/brand/tasur-icon-192.png",
    "pwa_icon_512": "/static/brand/tasur-icon-512.png",
    "pwa_icon_maskable_512": "/static/brand/tasur-icon-maskable-512.png",
}


_BRANDS: dict[str, Brand] = {
    "tasur": Brand(
        key="tasur",
        name="Tasur",
        application_name="Tasur",
        # Neutral on purpose: this surface serves every market that has no
        # module of its own, so it must not name a country, a tax regime or
        # a currency.
        meta_description="API-first accounting for small business.",
        market=None,
        demo_tagline="Small-business accounting",
        demo_features=(
            "Full double-entry ledger",
            "Invoices, bills & payments",
            "Tax-ready reporting",
            "Bank reconciliation",
        ),
        pwa_description=(
            "API-first accounting for small business — invoices, bills, "
            "payments and reporting. Self-hosted or hosted."
        ),
        pwa_lang="en",
        **_SELGE_ASSETS,  # type: ignore[arg-type]
    ),
    "tasur-ee": Brand(
        key="tasur-ee",
        name="Tasur",
        application_name="Tasur",
        meta_description="API-first accounting for Estonian small business.",
        market="EE",
        demo_tagline="Estonian small-business accounting",
        demo_features=(
            "Full double-entry ledger",
            "Invoices, bills & payments",
            "Käibemaks & KMD ready",
            "Bank reconciliation",
        ),
        pwa_description=(
            "API-first accounting for Estonian small business — invoices, "
            "bills, käibemaks and KMD. Self-hosted or hosted."
        ),
        pwa_lang="et",
        **_SELGE_ASSETS,  # type: ignore[arg-type]
    ),
    "saebooks": Brand(
        key="saebooks",
        name="SAE Books",
        application_name="SAE Books",
        meta_description="API-first accounting for Australian small business.",
        market="AU",
        wordmark_src="/static/sae-books-logo.png",
        wordmark_alt="SAE Books",
        favicon_32="/static/pwa/icons/favicon-32.png",
        favicon_16="/static/pwa/icons/favicon-16.png",
        apple_touch_icon="/static/pwa/icons/apple-touch-icon-180.png",
    ),
}

# Tasur is the product brand as of 2026-08-11. ``saebooks`` remains in the
# table above purely so a deployment can pin back to it with one env var.
_DEFAULT_BRAND_KEY = "tasur"


def _current_brand_key() -> str:
    return os.environ.get("SAEBOOKS_BRAND", _DEFAULT_BRAND_KEY).strip().lower() or _DEFAULT_BRAND_KEY


def current_brand() -> Brand:
    """Return the active Brand config for SAEBOOKS_BRAND (defaults to tasur).

    Unknown values fall back to the default rather than raising — a typo'd
    env var should degrade to the known-good brand, not 500 every page.
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
