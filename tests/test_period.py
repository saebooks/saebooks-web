"""Unit tests for saebooks_web.period — the period-picker date resolver.

Pure-function tests, no client/DB fixtures needed.
"""
from __future__ import annotations

from datetime import date

from saebooks_web import period


class TestFyBoundsContaining:
    def test_au_default_within_fy(self):
        # 2026-07-21 (today in this scenario) sits in the AU FY26-27 (1 Jul 2026 - 30 Jun 2027).
        start, end = period.fy_bounds_containing(date(2026, 7, 21), fin_year_start_month=7)
        assert start == date(2026, 7, 1)
        assert end == date(2027, 6, 30)

    def test_au_default_before_fy_start(self):
        # Jan-Jun sits in the FY that started the prior July.
        start, end = period.fy_bounds_containing(date(2026, 3, 1), fin_year_start_month=7)
        assert start == date(2025, 7, 1)
        assert end == date(2026, 6, 30)

    def test_matches_engine_current_fy_bounds(self):
        # Mirrors saebooks/api/v1/reports.py::_current_fy_bounds exactly for
        # the AU default — this is the drift-prevention test.
        for d in (date(2026, 7, 1), date(2026, 6, 30), date(2026, 12, 25), date(2027, 1, 1)):
            start, end = period.fy_bounds_containing(d, fin_year_start_month=7)
            if d.month >= 7:
                assert (start, end) == (date(d.year, 7, 1), date(d.year + 1, 6, 30))
            else:
                assert (start, end) == (date(d.year - 1, 7, 1), date(d.year, 6, 30))

    def test_calendar_year_fy(self):
        # An EE-style company whose FY is the calendar year.
        start, end = period.fy_bounds_containing(date(2026, 3, 1), fin_year_start_month=1)
        assert start == date(2026, 1, 1)
        assert end == date(2026, 12, 31)

    def test_leap_day_start_clamped(self):
        # fin_year_start_day=29 in a month with only 28 days (Feb, non-leap
        # target year) clamps down instead of raising.
        start, _end = period.fy_bounds_containing(
            date(2026, 3, 1), fin_year_start_month=2, fin_year_start_day=29
        )
        assert start == date(2026, 2, 28)


class TestSubtractOneYear:
    def test_normal(self):
        assert period.subtract_one_year(date(2026, 7, 21)) == date(2025, 7, 21)

    def test_leap_day_clamped(self):
        assert period.subtract_one_year(date(2024, 2, 29)) == date(2023, 2, 28)


class TestResolvePeriod:
    TODAY = date(2026, 7, 21)

    def test_this_fy(self):
        from_, to_, active = period.resolve_period(
            "this_fy", fin_year_start_month=7, today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-07-01", "2026-07-21", "this_fy")

    def test_last_fy(self):
        from_, to_, active = period.resolve_period(
            "last_fy", fin_year_start_month=7, today=self.TODAY
        )
        assert (from_, to_, active) == ("2025-07-01", "2026-06-30", "last_fy")

    def test_calendar_ytd(self):
        from_, to_, active = period.resolve_period(
            "calendar_ytd", fin_year_start_month=7, today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-01-01", "2026-07-21", "calendar_ytd")

    def test_trailing_12(self):
        from_, to_, active = period.resolve_period(
            "trailing_12", fin_year_start_month=7, today=self.TODAY
        )
        assert (from_, to_, active) == ("2025-07-21", "2026-07-21", "trailing_12")

    def test_this_quarter(self):
        from_, to_, active = period.resolve_period(
            "this_quarter", fin_year_start_month=7, today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-07-01", "2026-07-21", "this_quarter")

    def test_this_quarter_boundary_months(self):
        # Q1 Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec.
        cases = {
            date(2026, 2, 15): "2026-01-01",
            date(2026, 5, 1): "2026-04-01",
            date(2026, 9, 30): "2026-07-01",
            date(2026, 12, 31): "2026-10-01",
        }
        for d, expected_from in cases.items():
            from_, _to, _active = period.resolve_period("this_quarter", today=d)
            assert from_ == expected_from

    def test_custom_range_passthrough(self):
        from_, to_, active = period.resolve_period(
            "custom", "2026-01-01", "2026-03-31", today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-01-01", "2026-03-31", "custom")

    def test_none_preset_passthrough(self):
        from_, to_, active = period.resolve_period(
            None, "2026-01-01", "2026-03-31", today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-01-01", "2026-03-31", "custom")

    def test_unknown_preset_passthrough(self):
        from_, to_, active = period.resolve_period(
            "bogus", "2026-01-01", "2026-03-31", today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-01-01", "2026-03-31", "custom")

    def test_non_au_fin_year_start_month(self):
        # Calendar-year FY company (fin_year_start_month=1).
        from_, to_, active = period.resolve_period(
            "this_fy", fin_year_start_month=1, today=self.TODAY
        )
        assert (from_, to_, active) == ("2026-01-01", "2026-07-21", "this_fy")

    def test_last_fy_across_new_year(self):
        # today early in the AU FY (before 30 June) — "last FY" is two
        # calendar years back to one calendar year back.
        from_, to_, active = period.resolve_period(
            "last_fy", fin_year_start_month=7, today=date(2026, 3, 1)
        )
        assert (from_, to_, active) == ("2024-07-01", "2025-06-30", "last_fy")
