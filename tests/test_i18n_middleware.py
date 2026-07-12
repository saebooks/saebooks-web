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
