"""Runtime gettext core for the web layer (EE GUI prep, Packet 2a).

Per the scope's named landmine: the 61 ``Jinja2Templates`` envs are
module-level singletons constructed once at import time. Jinja2's
built-in ``jinja2.ext.i18n`` extension ships an ``install_gettext_translations``
helper that binds ONE ``Translations`` object onto the shared environment —
that binding is process-global, so under async concurrency user A's
request can render in user B's language (last writer wins across
interleaved coroutines on the same env). This module never calls that
helper.

Instead:
  - ``current_locale`` is a ``contextvars.ContextVar`` — isolated per
    asyncio task, so two concurrent requests never see each other's value.
    ``LocaleMiddleware`` (see ``saebooks_web.i18n.middleware``) sets it
    once per request from the negotiation order in the scope doc
    (session/cookie override -> Accept-Language -> jurisdiction default).
  - ``gettext``/``ngettext`` are plain functions, not bound to any env.
    They read ``current_locale`` at CALL TIME (i.e. at template-render
    time, not at env-construction time) and look up a ``Translations``
    object from ``_translations_for(locale)`` — a small process-lifetime
    cache keyed by locale code, loaded once per locale and safe to share
    for reads (``babel.support.Translations`` objects are read-only after
    load).
  - ``register_i18n_global`` registers these as Jinja globals via the
    existing security patch, exactly like ``register_brand_global`` /
    ``register_feature_global`` — no route module needs touching.

Templates use them exactly like Jinja's i18n extension:

    {{ _("Sign in") }}
    {{ gettext("Sign in") }}
    {{ ngettext("%(n)s item", "%(n)s items", count) }}
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path

from babel.support import NullTranslations, Translations

_logger = logging.getLogger(__name__)

#: Domain matches the .pot/.po/.mo basename used by babel.cfg + Makefile.
DOMAIN = "messages"

#: et/EE default, en, ru — matches the scope's three locale dirs.
SUPPORTED_LOCALES: tuple[str, ...] = ("en", "et", "ru")
DEFAULT_LOCALE = "en"

LOCALES_DIR = Path(__file__).resolve().parent / "locales"

#: Per-request active locale. Isolated per asyncio task by contextvars —
#: this is what makes call-time resolution concurrency-safe (see module
#: docstring). Default matches DEFAULT_LOCALE so any code path that reads
#: the callables outside a request (tests, scripts) still gets sane output
#: instead of raising.
current_locale: ContextVar[str] = ContextVar("saebooks_web_locale", default=DEFAULT_LOCALE)

#: Process-lifetime cache: locale code -> loaded Translations. Loaded once
#: per locale on first use, then reused for every subsequent request in
#: that locale — this is what "per-locale cached Translations" means in
#: the scope. Safe to share across concurrent requests: babel's
#: Translations objects are immutable after ``.load()``.
_translations_cache: dict[str, NullTranslations] = {}


def normalize_locale(locale: str | None) -> str:
    """Fold an arbitrary locale string down to one of SUPPORTED_LOCALES.

    Accepts full tags like "et-EE" or "ru-RU" (as sent by browsers via
    Accept-Language) by taking the primary subtag. Falls back to
    DEFAULT_LOCALE for anything unrecognised rather than raising — a bad
    cookie value or exotic Accept-Language entry should degrade, not 500.
    """
    if not locale:
        return DEFAULT_LOCALE
    primary = locale.strip().lower().split("-")[0].split("_")[0]
    return primary if primary in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _load_translations(locale: str) -> NullTranslations:
    try:
        translations = Translations.load(str(LOCALES_DIR), locales=[locale], domain=DOMAIN)
    except Exception:  # pragma: no cover — defensive, missing/corrupt .mo
        _logger.warning("i18n: failed loading translations for locale=%r", locale, exc_info=True)
        return NullTranslations()
    return translations


def _translations_for(locale: str) -> NullTranslations:
    """Return the cached Translations for ``locale``, loading it on first use.

    Loading is idempotent and cheap to repeat if two coroutines race on a
    cold cache (both would load and one write wins) — no lock needed
    because the loaded object is immutable and either copy is correct.
    """
    normalized = normalize_locale(locale)
    cached = _translations_cache.get(normalized)
    if cached is None:
        cached = _load_translations(normalized)
        _translations_cache[normalized] = cached
    return cached


def reset_translations_cache() -> None:
    """Drop all cached Translations so the next lookup reloads from disk.

    Used by tests after recompiling .mo files, and by the compile Make
    target's dev workflow (not needed in prod — catalogs don't change
    without a redeploy).
    """
    _translations_cache.clear()


def gettext(message: str) -> str:
    """Call-time translation lookup for the active request's locale.

    Reads ``current_locale`` fresh on every call (not once at env-build
    time) — this is what makes it safe to register on a shared,
    module-level singleton Jinja env.
    """
    return _translations_for(current_locale.get()).gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translations_for(current_locale.get()).ngettext(singular, plural, n)


def register_i18n_global(templates) -> None:
    """Register gettext callables as Jinja globals on a Jinja2Templates env.

    Called from the patched ``Jinja2Templates.__init__`` (see
    ``saebooks_web/security/__init__.py``) so every templates instance
    gets them automatically — mirrors ``register_brand_global`` /
    ``register_feature_global`` wiring exactly.

    Deliberately does NOT use ``jinja2.ext.i18n`` + ``install_gettext_translations``
    (the scope's named landmine — see module docstring): that pattern binds
    translations onto the env object itself, which is process-global for
    these singleton envs. Registering plain call-time-resolving functions
    as globals sidesteps that entirely — nothing is bound to the env,
    every call reads the current request's contextvar.
    """
    try:
        templates.env.globals.setdefault("gettext", gettext)
        templates.env.globals.setdefault("_", gettext)
        templates.env.globals.setdefault("ngettext", ngettext)
    except AttributeError:  # pragma: no cover — defensive
        _logger.warning("register_i18n_global: %r has no .env.globals", templates)


__all__ = [
    "DEFAULT_LOCALE",
    "DOMAIN",
    "LOCALES_DIR",
    "SUPPORTED_LOCALES",
    "current_locale",
    "gettext",
    "ngettext",
    "normalize_locale",
    "register_i18n_global",
    "reset_translations_cache",
]
