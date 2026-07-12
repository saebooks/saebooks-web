"""Concurrency proof for the gettext core (EE GUI prep Packet 2a).

The scope's named landmine: the 61 Jinja2Templates envs are module-level
singletons. Using jinja2.ext.i18n's install_gettext_translations() on a
shared env binds translations process-globally — under async concurrency,
user A's request can render in user B's language.

This test proves the actual chosen pattern (contextvar + call-time
per-locale cached Translations, both registered as plain Jinja globals —
see saebooks_web/i18n/__init__.py and middleware.py) does NOT have that
race: two "requests" running concurrently on the shared gettext/ngettext
functions, in different locales, each see only their own locale's
translation — even when deliberately interleaved via asyncio sleeps to
force the race window that install_gettext_translations() would lose.

A self-contained fixture catalog (not the real, still-empty app catalog —
the string sweep is a later gated packet) is compiled into a temp
locales dir so this test asserts on real, differing translated output
rather than an identity no-op.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo

import saebooks_web.i18n as i18n

FIXTURE_MSGID = "Sign in"
FIXTURE_TRANSLATIONS = {
    "et": "Logi sisse",
    "ru": "Войти",
    "en": "Sign in",
}


def _write_fixture_catalog(locales_dir: Path) -> None:
    for locale, translated in FIXTURE_TRANSLATIONS.items():
        catalog = Catalog(locale=locale, domain=i18n.DOMAIN)
        catalog.add(FIXTURE_MSGID, translated)
        lc_messages = locales_dir / locale / "LC_MESSAGES"
        lc_messages.mkdir(parents=True, exist_ok=True)
        with open(lc_messages / f"{i18n.DOMAIN}.mo", "wb") as fh:
            write_mo(fh, catalog)


@pytest.fixture
def fixture_locale_cache(tmp_path: Path):
    """Point i18n at a throwaway fixture catalog, then restore + evict.

    Deliberately does NOT use monkeypatch.setattr for LOCALES_DIR: pytest
    fixture teardown is LIFO, so a finalizer registered *inside* this test
    (including monkeypatch's own undo, which is queued when the built-in
    ``monkeypatch`` fixture is set up, before this fixture's body runs)
    would run monkeypatch's revert-of-LOCALES_DIR AFTER any reset we did
    here — leaving the process-lifetime ``_translations_cache`` holding
    this test's fixture Translations objects (keyed "et"/"ru") even once
    LOCALES_DIR is back to the real app catalog. The next test to call
    ``i18n.gettext`` for "et"/"ru" would silently get this fixture's
    "Sign in" -> "Logi sisse" entry instead of the real catalog — a
    cross-test leak. Manual save/restore here guarantees the reset happens
    strictly *after* LOCALES_DIR is back to the real path.
    """
    _write_fixture_catalog(tmp_path)
    original_dir = i18n.LOCALES_DIR
    i18n.LOCALES_DIR = tmp_path
    i18n.reset_translations_cache()
    try:
        yield
    finally:
        i18n.LOCALES_DIR = original_dir
        i18n.reset_translations_cache()


def test_gettext_resolves_per_locale(fixture_locale_cache):
    """Sanity check outside concurrency: each locale gets its own string."""
    token = i18n.current_locale.set("et")
    try:
        assert i18n.gettext(FIXTURE_MSGID) == "Logi sisse"
    finally:
        i18n.current_locale.reset(token)

    token = i18n.current_locale.set("ru")
    try:
        assert i18n.gettext(FIXTURE_MSGID) == "Войти"
    finally:
        i18n.current_locale.reset(token)


async def test_concurrent_requests_different_locales_same_env(fixture_locale_cache):
    """Two simulated concurrent requests, different locales, same shared
    gettext/ngettext callables (as registered on a single Jinja env by
    register_i18n_global) — must not cross-contaminate.

    Mirrors what LocaleMiddleware actually does per request: contextvar
    .set() at request start, application code (here: gettext calls with
    artificial awaits to force interleaving) runs, .reset() via the token
    on the way out — all inside one shared process/event loop, exactly
    the deployment topology the landmine warns about.
    """
    results: dict[str, list[str]] = {"et": [], "ru": []}

    async def simulate_request(locale: str, n_calls: int = 5) -> None:
        token = i18n.current_locale.set(locale)
        try:
            for _ in range(n_calls):
                # Yield control mid-"render" so the other simulated request's
                # gettext calls interleave on the shared event loop —
                # this is the exact race window install_gettext_translations()
                # would lose (last .set() on the shared env wins for both).
                await asyncio.sleep(0)
                results[locale].append(i18n.gettext(FIXTURE_MSGID))
        finally:
            i18n.current_locale.reset(token)

    await asyncio.gather(
        simulate_request("et"),
        simulate_request("ru"),
    )

    assert results["et"] == ["Logi sisse"] * 5
    assert results["ru"] == ["Войти"] * 5
    # And the ambient/default locale for anything outside a request scope
    # is untouched by either simulated request having run.
    assert i18n.current_locale.get() == i18n.DEFAULT_LOCALE


async def test_concurrent_requests_share_one_cached_translations_object(fixture_locale_cache):
    """Confirms the isolation comes from the contextvar, not from each
    "request" accidentally getting its own Translations instance — the
    per-locale cache (scope requirement: "cached Translations") really is
    shared and reused across concurrent callers of the same locale.
    """
    seen_ids: set[int] = set()

    async def simulate_request(locale: str) -> None:
        token = i18n.current_locale.set(locale)
        try:
            await asyncio.sleep(0)
            seen_ids.add(id(i18n._translations_for(i18n.current_locale.get())))
        finally:
            i18n.current_locale.reset(token)

    await asyncio.gather(
        simulate_request("et"),
        simulate_request("et"),
        simulate_request("et"),
    )

    assert len(seen_ids) == 1, "expected one cached Translations object reused across requests"
