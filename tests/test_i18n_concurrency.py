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


def _install_fixture_locales(monkeypatch, tmp_path: Path) -> None:
    _write_fixture_catalog(tmp_path)
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.reset_translations_cache()


def test_gettext_resolves_per_locale(monkeypatch, tmp_path):
    """Sanity check outside concurrency: each locale gets its own string."""
    _install_fixture_locales(monkeypatch, tmp_path)

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


async def test_concurrent_requests_different_locales_same_env(monkeypatch, tmp_path):
    """Two simulated concurrent requests, different locales, same shared
    gettext/ngettext callables (as registered on a single Jinja env by
    register_i18n_global) — must not cross-contaminate.

    Mirrors what LocaleMiddleware actually does per request: contextvar
    .set() at request start, application code (here: gettext calls with
    artificial awaits to force interleaving) runs, .reset() via the token
    on the way out — all inside one shared process/event loop, exactly
    the deployment topology the landmine warns about.
    """
    _install_fixture_locales(monkeypatch, tmp_path)

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


async def test_concurrent_requests_share_one_cached_translations_object(monkeypatch, tmp_path):
    """Confirms the isolation comes from the contextvar, not from each
    "request" accidentally getting its own Translations instance — the
    per-locale cache (scope requirement: "cached Translations") really is
    shared and reused across concurrent callers of the same locale.
    """
    _install_fixture_locales(monkeypatch, tmp_path)

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
