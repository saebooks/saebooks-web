"""Drop malformed date query parameters before any page sees them (b15, P2-7).

Report and list pages read dates straight from the query string and call
``date.fromisoformat`` on them; a hand-edited or truncated link
(``?to_date=2026-13-40``, ``?from=30/06/2026``) raised ``ValueError`` and the
page crashed with a server error.

This pure-ASGI middleware removes any date-named parameter whose value is
not a real ``YYYY-MM-DD`` date (an Australian ``dd/mm/yyyy`` value is
converted instead of dropped), so the page falls back to its normal default
period. The names it removed are put on ``scope["state"]["dropped_date_params"]``
and base.html shows one plain sentence saying so. Empty values are left
alone (pages already treat them as "not given").
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qsl, urlencode

#: Query parameter names that always carry a date in this app.
DATE_PARAM_NAMES = frozenset(
    {
        "from", "to", "from_date", "to_date", "date_from", "date_to",
        "as_of", "as_at", "as_of_date", "through", "through_date",
        "start", "end", "start_date", "end_date", "period_start", "period_end",
        "issue_date", "due_date", "on",
    }
)

_DMY = re.compile(r"^\s*(\d{1,2})[/.](\d{1,2})[/.](\d{4})\s*$")


def normalise_date(value: str) -> str | None:
    """ISO date string, or None when ``value`` is not a real date."""
    raw = value.strip()
    try:
        return date.fromisoformat(raw).isoformat() if len(raw) == 10 else None
    except ValueError:
        pass
    m = _DMY.match(raw)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def clean_query(query: str) -> tuple[str, list[str]]:
    """(cleaned query string, names dropped)."""
    if not query:
        return query, []
    pairs = parse_qsl(query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    changed = False
    for name, value in pairs:
        if name in DATE_PARAM_NAMES and value.strip():
            fixed = normalise_date(value)
            if fixed is None:
                dropped.append(name)
                changed = True
                continue
            if fixed != value:
                changed = True
            value = fixed
        kept.append((name, value))
    if not changed:
        return query, []
    return urlencode(kept), dropped


class DateParamMiddleware:
    """ASGI middleware — see module docstring."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") in ("GET", "HEAD"):
            raw = scope.get("query_string", b"")
            if raw:
                try:
                    query = raw.decode("latin-1")
                except Exception:
                    query = ""
                cleaned, dropped = clean_query(query)
                if cleaned != query:
                    if dropped:
                        # Mutate the shared state dict in place so every
                        # layer (and the template) sees the same object.
                        scope.setdefault("state", {})["dropped_date_params"] = dropped
                    scope = dict(scope)
                    scope["query_string"] = cleaned.encode("latin-1")
        await self.app(scope, receive, send)


#: Zone used for "today" in the screens when the deployment sets none.
DEFAULT_TIMEZONE = "Australia/Brisbane"


def apply_default_timezone() -> str:
    """Make ``date.today()`` the business's date, not the server's UTC date.

    Before 10 am in Queensland the UTC date is still yesterday, so every
    screen that defaulted a report or form to "today" showed yesterday
    (gap P2-7). The web app calls ``date.today()`` in many places; rather
    than thread a time zone through each one, the process time zone is set
    once at start-up: ``TZ`` if the deployment set it, else
    ``SAEBOOKS_WEB_TZ``, else Brisbane. The engine works out each company's
    own "today" separately (``services.company_clock``), so engine figures
    such as the needs-attention counts follow the company's own zone.
    Returns the zone in use.
    """
    import os
    import time

    zone = os.environ.get("TZ") or os.environ.get("SAEBOOKS_WEB_TZ") or DEFAULT_TIMEZONE
    if os.environ.get("TZ") != zone:
        os.environ["TZ"] = zone
    if hasattr(time, "tzset"):
        time.tzset()
    return zone
