"""LocaleMiddleware negotiation order (scope decision 2):
session/cookie override -> Accept-Language -> jurisdiction default.
"""
from __future__ import annotations

from types import SimpleNamespace

from saebooks_web.i18n.middleware import COOKIE_NAME, SESSION_LOCALE_KEY, resolve_locale


class _FakeRequest:
    def __init__(self, *, session=None, cookies=None, headers=None, jurisdiction=None):
        self.scope = {"session": {}} if session is not None else {}
        self.session = session or {}
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.state = SimpleNamespace(active_company_jurisdiction=jurisdiction)


def test_session_override_wins_over_everything():
    req = _FakeRequest(
        session={SESSION_LOCALE_KEY: "ru"},
        cookies={COOKIE_NAME: "et"},
        headers={"accept-language": "en"},
        jurisdiction="EE",
    )
    assert resolve_locale(req) == "ru"


def test_cookie_wins_over_accept_language_and_jurisdiction():
    req = _FakeRequest(cookies={COOKIE_NAME: "ru"}, headers={"accept-language": "en"}, jurisdiction="EE")
    assert resolve_locale(req) == "ru"


def test_accept_language_wins_over_jurisdiction_default():
    req = _FakeRequest(headers={"accept-language": "et-EE,et;q=0.9"}, jurisdiction="AU")
    assert resolve_locale(req) == "et"


def test_jurisdiction_default_ee_maps_to_et():
    req = _FakeRequest(jurisdiction="EE")
    assert resolve_locale(req) == "et"


def test_jurisdiction_default_au_maps_to_en():
    req = _FakeRequest(jurisdiction="AU")
    assert resolve_locale(req) == "en"


def test_no_signal_at_all_falls_back_to_default_locale():
    req = _FakeRequest()
    assert resolve_locale(req) == "en"


def test_unsupported_accept_language_falls_through_to_jurisdiction():
    # "fr" isn't a supported locale — must not silently claim it as "en"
    # via normalize_locale's fallback; must fall through to jurisdiction.
    req = _FakeRequest(headers={"accept-language": "fr-FR,fr;q=0.9"}, jurisdiction="EE")
    assert resolve_locale(req) == "et"


# ---------------------------------------------------------------------------
# Real middleware-stack ordering (critic round 2 regression).
#
# The tests above all inject request.state.active_company_jurisdiction
# directly, bypassing CompanyContextMiddleware entirely — they cannot catch
# an ordering bug between real middleware instances. This test asserts the
# actual invariant against the actual app: CompanyContextMiddleware (and,
# transitively, LocaleMiddleware) must run AFTER the session-minting auth
# middleware (TrustedHeaderAuthMiddleware / DemoAutoLoginMiddleware), so
# that request.session["api_token"] minted by auth on the FIRST request of
# a new SSO/demo session is already visible when CompanyContextMiddleware
# reads it — otherwise active_company_jurisdiction stays None on that
# request and LocaleMiddleware's jurisdiction fallback never fires for an
# EE company's very first authenticated page view.
# ---------------------------------------------------------------------------


def test_company_context_runs_after_session_minting_auth_middleware():
    from saebooks_web.company_context import CompanyContextMiddleware
    from saebooks_web.i18n.middleware import LocaleMiddleware
    from saebooks_web.main import app
    from saebooks_web.security.demo_autologin import DemoAutoLoginMiddleware
    from saebooks_web.security.trusted_header import TrustedHeaderAuthMiddleware

    # app.user_middleware is stored in Starlette's insert(0, ...) order:
    # index 0 is the most-recently-added middleware, which is also the
    # OUTERMOST wrapper and therefore executes FIRST per request (verified
    # live against this exact app — see saebooks_web/main.py's ordering
    # note). A LOWER index here means "executes earlier".
    names = [m.cls for m in app.user_middleware]

    def _index(cls):
        return names.index(cls)

    company_context_idx = _index(CompanyContextMiddleware)
    locale_idx = _index(LocaleMiddleware)
    trusted_header_idx = _index(TrustedHeaderAuthMiddleware)
    demo_autologin_idx = _index(DemoAutoLoginMiddleware)

    # Auth (token-minting) middleware must execute BEFORE CompanyContext
    # reads the session -> lower index = more outer = executes first, so
    # the minting middleware's index must be LOWER than CompanyContext's.
    assert trusted_header_idx < company_context_idx, (
        "TrustedHeaderAuthMiddleware must mint the session token before "
        "CompanyContextMiddleware reads it on the same request"
    )
    assert demo_autologin_idx < company_context_idx, (
        "DemoAutoLoginMiddleware must mint the session token before "
        "CompanyContextMiddleware reads it on the same request"
    )
    # Existing invariant (unchanged by this fix): CompanyContext still
    # resolves jurisdiction before LocaleMiddleware reads it.
    assert company_context_idx < locale_idx, (
        "CompanyContextMiddleware must set active_company_jurisdiction "
        "before LocaleMiddleware reads it for its jurisdiction fallback"
    )
