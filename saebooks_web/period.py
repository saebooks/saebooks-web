"""Period picker — resolves preset period selections into (from, to) dates.

Presets: ``this_fy``, ``last_fy``, ``calendar_ytd``, ``trailing_12``,
``this_quarter``. Anything else (``None``, ``"custom"``, or an unknown
value) is treated as a custom range and left to the caller's own
from/to-date handling.

"This FY" / "Last FY" derive from the company's financial-year *start
month* (``fin_year_start_month``, 1-12, default 7 for Australia) — every
financial year is assumed to start on the 1st of that month. The engine
does not yet store a start *day*; see
``~/records/saebooks/period-picker-engine-spec-2026-07-21.md`` for the
day-precision follow-up (engine field, gated on another lane's in-flight
migration work). Until that field exists, every FY boundary computed here
is day=1 of the start month — this module is written so that once the
engine field lands, only the call sites need to start passing the day
through (this module's ``resolve_period`` signature already anticipates it
via a keyword arg with the same day=1 default).

Deliberately independent of the AU GST/BAS financial year used by the
``ytd_turnover`` / PSI 80-20 tiles (hardcoded 1 Jul - 30 Jun in the engine,
because the $75k GST turnover threshold is an AU-specific concept). For an
AU company (``fin_year_start_month=7``, the default) the two coincide; for
a non-AU company (e.g. Estonia, calendar-year FY) they will not, by
design — the GST tile only ever applies to AU companies anyway.
"""
from __future__ import annotations

from datetime import date, timedelta

#: Preset identifiers accepted by ``resolve_period``. Anything else
#: (including ``None`` / ``"custom"``) falls through to a custom range.
PRESET_IDS: tuple[str, ...] = (
    "this_fy",
    "last_fy",
    "calendar_ytd",
    "trailing_12",
    "this_quarter",
)


def subtract_one_year(d: date) -> date:
    """Subtract exactly one calendar year from *d*, with a safe leap-day fallback.

    2025-02-28 -> 2024-02-28 (normal)
    2024-02-29 -> 2023-02-28 (29 Feb only exists in a leap year; clamp to 28 Feb)
    """
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def fy_bounds_containing(
    d: date, fin_year_start_month: int = 7, fin_year_start_day: int = 1
) -> tuple[date, date]:
    """Return (fy_start, fy_end) for the financial year containing *d*.

    The financial year starts on ``fin_year_start_day`` of
    ``fin_year_start_month`` (default 1 July, the AU default) and runs for
    exactly one year, ending the day before the next start date. Matches
    the engine's ``_current_fy_bounds`` (``saebooks/api/v1/reports.py``)
    exactly when ``fin_year_start_month=7, fin_year_start_day=1``.
    """
    start_month = fin_year_start_month if 1 <= (fin_year_start_month or 7) <= 12 else 7
    start_day = fin_year_start_day if fin_year_start_day and fin_year_start_day >= 1 else 1

    def _clamped(year: int) -> date:
        # Clamp an invalid day (e.g. day=31 in a start month with fewer
        # days) down to that month's last day rather than raising.
        import calendar as _calendar

        last_day = _calendar.monthrange(year, start_month)[1]
        return date(year, start_month, min(start_day, last_day))

    candidate = _clamped(d.year)
    if d >= candidate:
        fy_start = candidate
        fy_start_next = _clamped(d.year + 1)
    else:
        fy_start = _clamped(d.year - 1)
        fy_start_next = candidate
    fy_end = fy_start_next - timedelta(days=1)
    return fy_start, fy_end


async def fetch_fin_year_start_month(client) -> int:
    """Fetch the active company's fin_year_start_month via *client*.

    Returns 7 (the AU default) on any failure (network error, 401, empty
    company list, missing field) — callers should never have to special-case
    this beyond the returned int, matching every other "degrade softly"
    helper in this codebase.
    """
    try:
        resp = await client.get("/api/v1/companies", params={"limit": 1, "offset": 0})
        if resp.is_success:
            items = resp.json().get("items", [])
            if items:
                return items[0].get("fin_year_start_month") or 7
    except Exception:
        pass
    return 7


def resolve_period(
    preset: str | None,
    from_date: str | None = None,
    to_date: str | None = None,
    fin_year_start_month: int = 7,
    fin_year_start_day: int = 1,
    today: date | None = None,
) -> tuple[str, str, str]:
    """Resolve a preset (or an explicit custom range) to ISO date strings.

    Returns ``(from_date, to_date, active_preset)``. When *preset* is one
    of ``PRESET_IDS`` the *from_date*/*to_date* arguments are ignored and
    dates are computed relative to *today* (defaults to ``date.today()``).
    Otherwise the supplied *from_date*/*to_date* are returned verbatim
    (``None`` values pass through — callers apply their own defaults) with
    ``active_preset="custom"``.
    """
    d = today or date.today()

    if preset == "this_fy":
        fy_start, _fy_end = fy_bounds_containing(d, fin_year_start_month, fin_year_start_day)
        return fy_start.isoformat(), d.isoformat(), "this_fy"

    if preset == "last_fy":
        fy_start, _fy_end = fy_bounds_containing(d, fin_year_start_month, fin_year_start_day)
        prior_start, prior_end = fy_bounds_containing(
            fy_start - timedelta(days=1), fin_year_start_month, fin_year_start_day
        )
        return prior_start.isoformat(), prior_end.isoformat(), "last_fy"

    if preset == "calendar_ytd":
        return date(d.year, 1, 1).isoformat(), d.isoformat(), "calendar_ytd"

    if preset == "trailing_12":
        start = subtract_one_year(d)
        return start.isoformat(), d.isoformat(), "trailing_12"

    if preset == "this_quarter":
        q_month = ((d.month - 1) // 3) * 3 + 1
        return date(d.year, q_month, 1).isoformat(), d.isoformat(), "this_quarter"

    return from_date, to_date, "custom"
