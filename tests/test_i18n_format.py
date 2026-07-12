"""Tests for saebooks_web.i18n.format — money()/num()/fmt_date() globals
(EE GUI prep, Packet 4).

Byte-exact assertions are the point of this file (per the packet's AU
pixel-equivalence requirement): the babel gotcha this module exists to
dodge is that ``format_currency(x, "AUD", locale="en")`` renders
``"A$1,234.56"``, not the bare ``"$1,234.56"`` every AU page has always
shown — that only comes out of the *regional* locale "en_AU". These tests
assert the actual bytes babel emits (including its real U+00A0 non-breaking
spaces in the et/ru output), not a hand-typed approximation, so a babel
upgrade that changes CLDR data would fail this file loudly instead of
shipping a silent AU regression.
"""
from __future__ import annotations

from saebooks_web.i18n import current_locale
from saebooks_web.i18n.format import (
    current_currency,
    fmt_date,
    money,
    num,
    resolve_currency,
)

_NBSP = "\xa0"


def _run_as(locale: str, currency: str, fn, *args, **kwargs):
    locale_token = current_locale.set(locale)
    currency_token = current_currency.set(currency)
    try:
        return fn(*args, **kwargs)
    finally:
        current_locale.reset(locale_token)
        current_currency.reset(currency_token)


# ---------------------------------------------------------------------------
# money() — AU byte-equality is the load-bearing assertion in this file.
# ---------------------------------------------------------------------------


def test_money_au_matches_todays_dollar_format_exactly():
    # This is the literal string every existing AU `${{ "%.2f"|format(x) }}`
    # / `${{ "{:,.2f}".format(x) }}` call site renders today.
    assert _run_as("en", "AUD", money, 1234.56) == "$1,234.56"


def test_money_au_small_amount_no_grouping():
    assert _run_as("en", "AUD", money, 5) == "$5.00"


def test_money_au_zero():
    assert _run_as("en", "AUD", money, 0) == "$0.00"


def test_money_au_none_treated_as_zero():
    # Several call sites pass `x or 0` style values through; money() must
    # not blow up on None.
    assert _run_as("en", "AUD", money, None) == "$0.00"


def test_money_au_negative():
    assert _run_as("en", "AUD", money, -42.5) == "-$42.50"


def test_money_ee_uses_comma_decimal_and_trailing_symbol():
    # Real babel bytes: U+00A0 (non-breaking space) as both the thousands
    # separator and the symbol separator, not a plain ASCII space.
    assert _run_as("et", "EUR", money, 1234.56) == f"1{_NBSP}234,56{_NBSP}€"


def test_money_ee_small_amount():
    assert _run_as("et", "EUR", money, 5) == f"5,00{_NBSP}€"


def test_money_ru_locale_uses_comma_decimal_too():
    assert _run_as("ru", "EUR", money, 1234.56) == f"1{_NBSP}234,56{_NBSP}€"


def test_money_explicit_ccy_overrides_current_currency():
    # Foreign-currency bill/PO/invoice display: company is AU/AUD but the
    # specific document is in EUR. Real babel output for this combination
    # is the ugly "EUR1,234.56" (en_AU locale data has no EUR symbol
    # mapping) — verified live, not assumed. This is exactly why P4 leaves
    # the existing `{{ bill.currency }} {{ "%.2f"|format(x) }}` FX display
    # convention on bills/POs/invoices unconverted (see the packet report):
    # money() is correct for the home-currency case this packet sweeps,
    # not yet a drop-in replacement for the FX case.
    assert _run_as("en", "AUD", money, 1234.56, ccy="EUR") == "EUR1,234.56"


def test_money_decimals_override_preserves_precision_au():
    # employees/detail.html + employees/list.html base_rate — payroll needs
    # 4dp, not the currency's natural 2dp (see i18n/format.py money()
    # docstring: this is why decimals exists at all).
    assert _run_as("en", "AUD", money, 28.8462, decimals=4) == "$28.8462"


def test_money_decimals_override_ee():
    assert _run_as("et", "EUR", money, 28.8462, decimals=4) == f"28,8462{_NBSP}€"


def test_money_decimals_override_negative():
    assert _run_as("en", "AUD", money, -28.8462, decimals=4) == "-$28.8462"


def test_money_ee_operator_viewing_au_company_still_gets_aud_symbol():
    # Locale (UI language) and currency (company jurisdiction) are
    # independent axes — an et-speaking user on an AU company sees AUD
    # formatted with et number conventions, not $ replaced by €.
    assert _run_as("et", "AUD", money, 1234.56) == f"1{_NBSP}234,56{_NBSP}AU$"


# ---------------------------------------------------------------------------
# num() — non-currency numbers (percentages, plain counts).
# ---------------------------------------------------------------------------


def test_num_au_default_two_decimals():
    assert _run_as("en", "AUD", num, 1234.5) == "1,234.50"


def test_num_au_zero_decimals_for_whole_number_tiles():
    assert _run_as("en", "AUD", num, 1234.6, decimals=0) == "1,235"


def test_num_au_one_decimal_for_percentage_sites():
    assert _run_as("en", "AUD", num, 12.34, decimals=1) == "12.3"


def test_num_ee_uses_comma_decimal():
    assert _run_as("et", "EUR", num, 1234.5) == f"1{_NBSP}234,50"


def test_num_none_treated_as_zero():
    assert _run_as("en", "AUD", num, None, decimals=0) == "0"


# ---------------------------------------------------------------------------
# fmt_date() — infra for this packet; not swept into any template call site
# yet (see module docstring), but covered directly.
# ---------------------------------------------------------------------------


def test_fmt_date_au_short():
    assert _run_as("en", "AUD", fmt_date, "2026-07-12") == "12/7/26"


def test_fmt_date_ee_short_uses_dot_separated_day_month_year():
    assert _run_as("et", "EUR", fmt_date, "2026-07-12") == "12.07.26"


def test_fmt_date_empty_value_returns_empty_string():
    assert _run_as("en", "AUD", fmt_date, None) == ""
    assert _run_as("en", "AUD", fmt_date, "") == ""


def test_fmt_date_bad_string_returned_verbatim_not_raised():
    assert _run_as("en", "AUD", fmt_date, "not-a-date") == "not-a-date"


# ---------------------------------------------------------------------------
# resolve_currency() — jurisdiction -> home currency, pure function.
# ---------------------------------------------------------------------------


def test_resolve_currency_au():
    assert resolve_currency("AU") == "AUD"


def test_resolve_currency_ee():
    assert resolve_currency("EE") == "EUR"


def test_resolve_currency_unknown_defaults_au():
    assert resolve_currency("XX") == "AUD"
    assert resolve_currency(None) == "AUD"


def test_resolve_currency_case_insensitive():
    assert resolve_currency("ee") == "EUR"
