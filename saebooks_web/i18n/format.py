"""Locale-aware money/number/date formatting Jinja globals (EE GUI prep, Packet 4).

Mirrors ``saebooks_web/i18n/__init__.py`` exactly, including its named
concurrency landmine and the fix for it: the 61 ``Jinja2Templates`` envs are
module-level singletons, so nothing may be bound onto the env itself.
Instead ``money``/``num``/``fmt_date`` are plain functions that read a
``contextvars.ContextVar`` at CALL TIME (template-render time), registered
as Jinja globals via the same ``security/__init__.py`` injection hook that
already wires ``gettext``/``_``/``ngettext``, ``current_edition``,
``is_feature_enabled`` and ``current_brand``.

Currency negotiation (new for this packet) is simpler than locale
negotiation: unlike language, currency is not a durable per-user
preference — it is the active company's home currency, which for the
AU/EE binary this app supports today is fully determined by
``request.state.active_company_jurisdiction`` (AU -> AUD, EE -> EUR).
``LocaleMiddleware`` (see ``i18n/middleware.py``) already resolves that
jurisdiction for its own locale-default fallback, in the correct
middleware position (inside CompanyContextMiddleware); this module's
``resolve_currency`` is called from the same dispatch rather than adding a
second middleware layer that would have to be kept in the same position.
A real per-company ``base_currency`` (for multi-currency beyond the AU/EE
binary) is future work — same style of deferral as the durable
language-preference column noted in i18n/__init__.py.

Babel gotcha this module exists to get right (verified live, not assumed):
``format_currency(x, "AUD", locale="en")`` renders ``"A$1,234.56"`` — the
bare ISO-639 code "en" pulls in the ambiguous-currency "A$" disambiguation
babel/CLDR uses for International English. The app's existing AU pages
render a bare ``"$1,234.56"``, which only comes out of babel's *regional*
locale ``"en_AU"``. ``_FORMAT_LOCALE_MAP`` below is exactly that
translation-locale -> formatting-locale fix-up; every call into babel goes
through ``_format_locale()``, never through ``current_locale`` directly.
"""
from __future__ import annotations

import datetime as _dt
import logging
from contextvars import ContextVar

from babel.dates import format_date as _babel_format_date
from babel.numbers import format_currency as _babel_format_currency
from babel.numbers import format_decimal as _babel_format_decimal

from saebooks_web.i18n import current_locale

_logger = logging.getLogger(__name__)

#: Translation locale (SUPPORTED_LOCALES in i18n/__init__.py) -> babel
#: *formatting* locale. See module docstring — "en" alone renders AUD as
#: "A$", not the "$" the AU app has always shown; "en_AU" is what produces
#: bare "$". et/ru formatting is identical under the bare vs regional tag
#: (verified live), but the regional tag is used for all three for
#: consistency/future-proofing rather than relying on that being permanent
#: CLDR behaviour.
_FORMAT_LOCALE_MAP: dict[str, str] = {
    "en": "en_AU",
    "et": "et_EE",
    "ru": "ru_RU",
}

#: Jurisdiction -> home currency. Mirrors i18n/middleware.py's
#: _JURISDICTION_DEFAULT_LOCALE table exactly (same data shape, same
#: reasoning): AU/EE are the only two jurisdictions this app supports today
#: (see company_context.py). Unknown/missing jurisdiction falls through to
#: DEFAULT_CURRENCY.
_JURISDICTION_DEFAULT_CURRENCY: dict[str, str] = {
    "AU": "AUD",
    "EE": "EUR",
}
DEFAULT_CURRENCY = "AUD"

#: Per-request active currency. Isolated per asyncio task by contextvars —
#: same mechanism, same reasoning as i18n/__init__.py's current_locale.
current_currency: ContextVar[str] = ContextVar(
    "saebooks_web_currency", default=DEFAULT_CURRENCY
)


def _format_locale() -> str:
    """The babel *formatting* locale for the active request's UI language."""
    return _FORMAT_LOCALE_MAP.get(current_locale.get(), "en_AU")


def resolve_currency(jurisdiction: str | None) -> str:
    """Pure negotiation logic, exposed separately for tests.

    Mirrors i18n/middleware.py's resolve_locale jurisdiction fallback: no
    session/cookie override exists for currency (it is not a user
    preference — see module docstring), so this is a one-step lookup.
    """
    if jurisdiction:
        mapped = _JURISDICTION_DEFAULT_CURRENCY.get(jurisdiction.upper())
        if mapped:
            return mapped
    return DEFAULT_CURRENCY


def _decimal_pattern(decimals: int) -> str:
    """Build a babel numeric pattern with an explicit fraction-digit count.

    babel's ``format_currency`` quantizes to the *currency's* CLDR minor
    unit count (2 for AUD/EUR) regardless of a custom ``format=`` pattern
    passed to it — verified live, not assumed — so decimals-truncation
    cannot be done through format_currency. ``format_decimal`` has no such
    quirk: passing a pattern here directly controls rounding + digit
    count, which is what num()'s ``decimals`` param uses.
    """
    if decimals <= 0:
        return "#,##0"
    return "#,##0." + "0" * decimals


def money(value: float | int | str | None, ccy: str | None = None) -> str:
    """Locale-aware currency amount: symbol + grouping + decimal separator.

    ``ccy`` overrides the active company's home currency (current_currency)
    for foreign-currency displays (bills/POs/invoices in a non-home
    currency); omit it for the normal case.

    Deliberately always renders at the currency's natural precision (2dp
    for AUD/EUR) rather than accepting a decimals override — see the
    P4 commit notes: several existing whole-dollar tiles (`%.0f` with no
    cents) get cents added by this normalisation. That's a disclosed,
    intentional standardisation onto the same 2dp convention already used
    by other money tiles in this app (e.g. templates/overviews/expenses.html
    `{:,.2f}` sites), not a silent behaviour change — money should not be
    display-truncated. See the P4 report for the exact list of sites this
    affects.
    """
    amount = float(value or 0)
    currency_code = (ccy or current_currency.get()).upper()
    try:
        return _babel_format_currency(amount, currency_code, locale=_format_locale())
    except Exception:  # pragma: no cover — defensive, bad currency code etc.
        _logger.warning("money(): format_currency failed for %r %r", amount, currency_code, exc_info=True)
        return f"{amount:.2f}"


def num(value: float | int | str | None, decimals: int = 2) -> str:
    """Locale-aware plain number: grouping + decimal separator, no currency.

    Used for percentages (keep the literal ``%`` in the template) and for
    non-currency counts. ``decimals`` mirrors the ``%.Nf`` precision of the
    call site being replaced.
    """
    amount = float(value or 0)
    try:
        return _babel_format_decimal(
            amount, format=_decimal_pattern(decimals), locale=_format_locale()
        )
    except Exception:  # pragma: no cover — defensive
        _logger.warning("num(): format_decimal failed for %r", amount, exc_info=True)
        return f"{amount:.{max(decimals, 0)}f}"


def fmt_date(value: _dt.date | _dt.datetime | str | None, format: str = "short") -> str:
    """Locale-aware date. Accepts a date/datetime or an ISO 'YYYY-MM-DD' string.

    Delivered as infrastructure for this packet (registered global,
    covered by unit tests) but NOT swept into any template call site: the
    codebase's existing date fields are rendered as raw ISO passthrough
    (`{{ invoice.issue_date }}`), not via any `|format`/strftime call, so
    there is no template convention to replace without *introducing* a
    visible format change on AU pages that don't have one today (which
    would violate the AU pixel-equivalence requirement). See the P4
    report.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = _dt.date.fromisoformat(value[:10])
        except ValueError:
            return value
    try:
        return _babel_format_date(value, format=format, locale=_format_locale())
    except Exception:  # pragma: no cover — defensive
        _logger.warning("fmt_date(): format_date failed for %r", value, exc_info=True)
        return str(value)


def register_format_globals(templates) -> None:
    """Register money/num/fmt_date as Jinja globals on a Jinja2Templates env.

    Called from the patched ``Jinja2Templates.__init__`` (see
    ``saebooks_web/security/__init__.py``) — mirrors
    ``register_i18n_global``/``register_brand_global``/
    ``register_feature_global`` wiring exactly.
    """
    try:
        templates.env.globals.setdefault("money", money)
        templates.env.globals.setdefault("num", num)
        templates.env.globals.setdefault("fmt_date", fmt_date)
    except AttributeError:  # pragma: no cover — defensive
        _logger.warning("register_format_globals: %r has no .env.globals", templates)


__all__ = [
    "DEFAULT_CURRENCY",
    "current_currency",
    "fmt_date",
    "money",
    "num",
    "register_format_globals",
    "resolve_currency",
]
