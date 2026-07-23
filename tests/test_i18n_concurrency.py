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

Fixer round 4: the tests above only prove contextvar isolation under a
bare ``asyncio.gather`` of sibling coroutines — a stdlib guarantee, not a
proof of anything specific to this app. They never construct a real
Request, never call ``LocaleMiddleware.dispatch``, and never go through
Starlette's ``BaseHTTPMiddleware.call_next`` — which is where the
task-spawn-and-context-copy behaviour this design actually relies on
lives (``call_next`` runs downstream code via
``task_group.start_soon(coro)``, a genuinely separate anyio task, not the
same coroutine — see the module docstring fix in
``saebooks_web/i18n/middleware.py``). The two tests at the bottom of this
file close that gap: concurrent *real* HTTP requests through the actual
ASGI app (``httpx.ASGITransport`` + ``asyncio.gather``), exercising
``LocaleMiddleware`` and ``CompanyContextMiddleware`` for real, with
different locales/jurisdictions in flight at once.
"""
from __future__ import annotations

import asyncio
import json as _json
from base64 import b64encode as _b64encode
from pathlib import Path

import pytest
import respx
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

import saebooks_web.i18n as i18n
from saebooks_web.config import settings
from saebooks_web.main import app

from tests.test_dashboard import _register_mocks
from tests.test_jurisdiction_gating import _AU_COMPANY, _EE_COMPANY

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


# ---------------------------------------------------------------------------
# Real end-to-end proof (fixer round 4): actual ASGI requests through the
# real app, actual LocaleMiddleware/CompanyContextMiddleware, actual
# Starlette BaseHTTPMiddleware.call_next task-spawn path — not a simulated
# coroutine. This is the mechanism the module docstrings above only
# *describe*; these tests are the thing that would fail if the wiring
# regressed (e.g. LocaleMiddleware reordered, or its .set() calls moved to
# no longer run synchronously immediately before call_next).
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")


def _session_cookie(token: str) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps({"api_token": token}).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


@pytest.mark.anyio
async def test_concurrent_real_requests_login_different_accept_language() -> None:
    """20 interleaved real GET /login requests, alternating et/ru
    Accept-Language, fired together via asyncio.gather over one shared
    AsyncClient/ASGITransport (i.e. one shared event loop + one shared
    app, exactly the deployment topology the landmine warns about). Each
    response must contain only its own locale's real .po translation of
    the login subtitle and never the other locale's — proving isolation
    through the actual LocaleMiddleware.dispatch -> call_next -> anyio
    task-spawn path, not a simulated one.
    """
    et_text = "Sisesta oma"  # "Enter your %(brand)s email address and password."
    ru_text = "Введите свой адрес"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        async def _get(locale: str):
            return locale, await client.get(
                "/login?form=1", headers={"Accept-Language": f"{locale}-EE,{locale};q=0.9"}
            )

        results = await asyncio.gather(*[
            _get("et" if i % 2 == 0 else "ru") for i in range(20)
        ])

    for locale, resp in results:
        assert resp.status_code == 200
        if locale == "et":
            assert et_text in resp.text
            assert ru_text not in resp.text
        else:
            assert ru_text in resp.text
            assert et_text not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_concurrent_real_requests_dashboard_au_vs_ee_jurisdiction(
    respx_mock: respx.MockRouter,
) -> None:
    """Concurrent real GET / for two different logged-in sessions — one an
    AU company, one an EE company — fired together via asyncio.gather.
    The upstream /companies and /tax_codes mocks are keyed off the
    Authorization bearer header (each session's own api_token), exactly
    like company_context.py's real per-request upstream calls, so this
    proves the whole chain (LocaleMiddleware + CompanyContextMiddleware +
    money()/nav rendering) doesn't cross-contaminate two requests in
    flight at once — not just that contextvars are isolated in isolation.
    """
    from tests import _jp
    _jp.mock_presentations(respx_mock)

    au_token, ee_token = "concurrency-au-token", "concurrency-ee-token"

    def _companies(request: respx.Request) -> Response:
        auth = request.headers.get("authorization", "")
        company = _EE_COMPANY if ee_token in auth else _AU_COMPANY
        return Response(200, json={"items": [company], "total": 1})

    def _tax_codes(request: respx.Request) -> Response:
        auth = request.headers.get("authorization", "")
        jurisdiction = "EE" if ee_token in auth else "AU"
        items = [{
            "id": "aaaaaaaa-0000-0000-0000-000000000001",
            "code": "T1", "name": "Test code", "rate": "10.000",
            "tax_system": "VAT" if jurisdiction == "EE" else "GST",
            "jurisdiction": jurisdiction,
        }]
        return Response(200, json={"items": items, "total": 1})

    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$").mock(side_effect=_companies)
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_codes(\?.*)?$").mock(side_effect=_tax_codes)
    _register_mocks(respx_mock, register_shared_side_fetches=False)

    au_cookie = _session_cookie(au_token)
    ee_cookie = _session_cookie(ee_token)

    async def _get(cookie: str):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={settings.session_cookie_name: cookie},
        ) as client:
            return await client.get("/")

    results = await asyncio.gather(*[
        _get(au_cookie if i % 2 == 0 else ee_cookie) for i in range(10)
    ])

    for i, resp in enumerate(results):
        assert resp.status_code == 200
        if i % 2 == 0:
            assert "BAS worksheet" in resp.text
        else:
            assert "BAS worksheet" not in resp.text
