"""features.current_edition(request) — M2 app-lane step 9a unit tests.

(a) no module_usage in session → env-var fallback (regression guard);
(b) module_usage present → effective_edition wins over the env var;
(c) no request at all (legacy call) → env-var answer.
The end-to-end badge assertion lives in test_sidebar_module_nav.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from saebooks_web.features import current_edition


def _request(session: dict | None) -> SimpleNamespace:
    scope = {"session": session} if session is not None else {}
    return SimpleNamespace(scope=scope, session=session or {})


def test_no_usage_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "business")
    assert current_edition(_request({})) == "business"


def test_usage_effective_edition_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "community")
    req = _request({"module_usage": {"effective_edition": "Pro"}})
    assert current_edition(req) == "pro"


def test_usage_without_effective_edition_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "enterprise")
    req = _request({"module_usage": {"effective_edition": None}})
    assert current_edition(req) == "enterprise"


def test_legacy_no_arg_call_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "offline")
    assert current_edition() == "offline"


def test_request_without_session_scope_uses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAEBOOKS_EDITION", "community")
    assert current_edition(_request(None)) == "community"
