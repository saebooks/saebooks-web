"""Currency context for templates (batch 10).

``fx_context(request)`` is a Jinja global (registered on every template
environment by ``security._patch_jinja_templates``) giving the forms the
company currency, the currency list for the picker and whether foreign
currency is part of the company's plan. It needs no engine call: the
company currency comes from the company list the context middleware has
already fetched, and the currency list is the same fixed list the engine
serves at ``/api/v1/fx/currencies``. The engine stays the authority — a
foreign document on a plan without foreign currency is refused there.
"""
from __future__ import annotations

from typing import Any

#: Same order and names as the engine's ``api/v1/fx.py`` CURRENCY_NAMES.
CURRENCIES: tuple[tuple[str, str], ...] = (
    ("AUD", "Australian dollar"),
    ("USD", "United States dollar"),
    ("EUR", "Euro"),
    ("GBP", "British pound"),
    ("NZD", "New Zealand dollar"),
    ("JPY", "Japanese yen"),
    ("CNY", "Chinese yuan"),
    ("SGD", "Singapore dollar"),
    ("HKD", "Hong Kong dollar"),
    ("CAD", "Canadian dollar"),
    ("CHF", "Swiss franc"),
    ("INR", "Indian rupee"),
    ("IDR", "Indonesian rupiah"),
    ("KRW", "South Korean won"),
    ("MYR", "Malaysian ringgit"),
    ("PGK", "Papua New Guinean kina"),
    ("PHP", "Philippine peso"),
    ("THB", "Thai baht"),
    ("TWD", "New Taiwan dollar"),
    ("VND", "Vietnamese dong"),
    ("AED", "United Arab Emirates dirham"),
    ("ZAR", "South African rand"),
    ("SEK", "Swedish krona"),
    ("NOK", "Norwegian krone"),
    ("DKK", "Danish krone"),
    ("FJD", "Fijian dollar"),
)

#: Plans without foreign currency (engine FLAG_MULTI_CURRENCY is Offline+).
_NO_FX_EDITIONS = frozenset({"community"})


def company_currency(request: Any) -> str:
    state = getattr(request, "state", None)
    active_id = getattr(state, "active_company_id", None)
    for company in getattr(state, "companies", None) or []:
        if isinstance(company, dict) and str(company.get("id")) == str(active_id):
            base = company.get("base_currency")
            if base and str(base).upper() != "XXX":
                return str(base).upper()
    jp = getattr(state, "jp", None)
    return (getattr(jp, "default_currency", "") or "AUD").upper()


def fx_context(request: Any) -> dict[str, Any]:
    base = company_currency(request)
    try:
        from saebooks_web.features import current_edition

        enabled = current_edition(request) not in _NO_FX_EDITIONS
    except Exception:
        enabled = False
    codes = [c for c, _ in CURRENCIES]
    items = [{"code": c, "name": n} for c, n in CURRENCIES]
    if base not in codes:
        items.insert(0, {"code": base, "name": base})
    # Company currency first.
    items.sort(key=lambda i: i["code"] != base)
    return {"base": base, "currencies": items, "enabled": enabled}


def contact_currencies(contacts: list[dict] | None) -> dict[str, str]:
    """``{contact_id: billing currency}`` for contacts that have one."""
    out: dict[str, str] = {}
    for c in contacts or []:
        code = (c.get("currency_code") or "").strip().upper() if isinstance(c, dict) else ""
        if code:
            out[str(c.get("id"))] = code
    return out


def register_fx_global(templates: Any) -> None:
    templates.env.globals.setdefault("fx_context", fx_context)
    templates.env.globals.setdefault("contact_currencies", contact_currencies)


__all__ = [
    "CURRENCIES",
    "company_currency",
    "contact_currencies",
    "fx_context",
    "register_fx_global",
]
