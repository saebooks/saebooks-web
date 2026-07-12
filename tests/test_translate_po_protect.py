"""Tests for tools/translate_po.py's protect()/unprotect() (fixer round 4).

Bug: PROTECTED-term matching was case-sensitive literal substring, and KMV
was entirely absent from the list (only KMD, a different statutory term,
was present). Capitalised sentence-start occurrences of "Registrikood"
(the field label, both validation errors) and every occurrence of "KMV"
went through TartuNLP unprotected and came back mistranslated — verified
against the shipped messages.mo: msgid "Registrikood" -> msgstr
"Регистрация" ("Registration") in ru.

These tests exercise protect()/unprotect() directly (no network call —
TartuNLP is never hit here) against the exact real msgids from
saebooks_web/i18n/locales/en/LC_MESSAGES/messages.po that the finding
named as broken.
"""
from __future__ import annotations

import pytest

from tools.translate_po import PROTECTED, protect, tokens_lost, unprotect

# The real msgids this bug affected (companies/new.html EE onboarding).
_REAL_MSGIDS = [
    "Registrikood",
    "Registrikood is required for an Estonian company.",
    "Registrikood must be exactly 8 digits.",
    'KMV/VAT number must be "EE" followed by 9 digits, e.g. EE123456789.',
    "KMV / VAT number",
    "Company created. Estonian jurisdiction, registrikood and KMV/VAT "
    "number were captured but could not be saved yet.",
]


@pytest.mark.parametrize("msgid", _REAL_MSGIDS)
def test_protect_unprotect_round_trips_real_msgids(msgid: str) -> None:
    """protect() then unprotect() with no MT in between must be a no-op —
    the baseline every one of these msgids failed before this fix (the
    capitalised/KMV occurrences weren't tokenized at all, so they were
    never actually a no-op risk from protect/unprotect itself, but they
    also weren't shielded — this test's real job is the assertions below,
    which pin down that every occurrence IS now tokenized)."""
    protected = protect(msgid)
    assert unprotect(protected) == msgid


def test_capitalised_registrikood_is_tokenized() -> None:
    """The specific reported gap: sentence-start "Registrikood" (capital R)
    must be masked before hitting TartuNLP, not just lowercase
    "registrikood"."""
    protected = protect("Registrikood is required for an Estonian company.")
    assert "registrikood" not in protected.lower() or "NX3" in protected
    assert "Registrikood" not in protected


def test_kmv_is_now_a_protected_term() -> None:
    """KMV was entirely absent from PROTECTED — must be tokenized now."""
    assert any(term == "KMV" for term, _tok in PROTECTED)
    protected = protect("KMV / VAT number")
    assert "KMV" not in protected


def test_lowercase_registrikood_still_protected_unchanged() -> None:
    """Regression guard: the pre-existing lowercase-mid-sentence case this
    script already handled must keep working."""
    protected = protect("registrikood and KMV/VAT number")
    assert "registrikood" not in protected
    assert "KMV" not in protected
    assert unprotect(protected) == "registrikood and KMV/VAT number"


def test_capitalised_and_lowercase_forms_restore_with_correct_casing() -> None:
    """A single string with both casings of the same term must restore
    each occurrence with its own original casing, not collapse to one."""
    src = "Registrikood: enter your registrikood below."
    protected = protect(src)
    assert unprotect(protected) == src


def test_tokens_lost_still_detects_a_dropped_token() -> None:
    """tokens_lost()'s plain substring check must still work against the
    capitalised-suffix token form (NX3CAP contains NX3)."""
    protected = protect("Registrikood is required.")
    # Simulate TartuNLP dropping the placeholder entirely.
    mangled = "Something else entirely."
    lost = tokens_lost(protected, mangled)
    assert ("registrikood", "NX3") in lost
