"""Jurisdiction presentation contract — the web side.

Fetches ``GET /api/v1/jurisdictions/{code}/presentation`` (unauthenticated
static catalogue) and exposes it to templates as ``jp(request)`` so a template
renders the active company's identifier label/format FROM the country module —
no ``{% if jurisdiction == 'AU' %}`` branch. Same shape of solution as
``module_registry.py`` (process-TTL cache) and ``brand.py`` (Jinja global).

The active jurisdiction code is already resolved onto
``request.state.active_company_jurisdiction`` by ``company_context.py``; this
layer only turns that code into the presentation the GUI draws.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from starlette.requests import Request

from saebooks_web.api_client import api_client

_log = logging.getLogger("saebooks_web.jurisdiction_presentation")

_TTL_SECONDS = 600.0
# Cache per jurisdiction CODE — AU and EE return different contracts, so a
# single-slot cache (like the module catalogue) would be wrong here.
_cache: dict[str, tuple[float, "JurisdictionPresentation"]] = {}

# The neutral fallback the web renders if the engine is unreachable or the code
# is unknown — mirrors the engine's NEUTRAL_PRESENTATION so a fetch failure
# degrades to a generic label instead of a blank or a crash.
_NEUTRAL_LABEL = "Registration number"


@dataclass(frozen=True)
class Identifier:
    scheme: str
    label: str
    format_hint: str = ""
    optional: bool = False


@dataclass(frozen=True)
class BankField:
    key: str
    label: str
    format_hint: str = ""
    optional: bool = False


@dataclass(frozen=True)
class Tax:
    term: str = "Tax"
    return_name: str = "Tax return"
    registration_term: str = "Tax registration"


@dataclass(frozen=True)
class JurisdictionPresentation:
    primary_identifier: Identifier
    bank_fields: tuple[BankField, ...] = ()
    tax: Tax = Tax()

    @property
    def identifier_label(self) -> str:
        return self.primary_identifier.label


# Neutral fallback mirrors the engine's NEUTRAL_PRESENTATION.
_NEUTRAL = JurisdictionPresentation(
    Identifier(scheme="generic_business_id", label=_NEUTRAL_LABEL, optional=True),
    bank_fields=(
        BankField(key="bank_account_number", label="Account number", optional=True),
        BankField(key="bank_account_title", label="Account holder", optional=True),
    ),
    tax=Tax(),
)


async def fetch_presentation(request: Request, code: str | None) -> JurisdictionPresentation:
    """The presentation contract for a jurisdiction code, process-TTL-cached
    per code. Degrades to the neutral contract on any error — never raises,
    because a presentation lookup must not be able to white-screen a page."""
    key = (code or "").upper()
    if not key:
        return _NEUTRAL
    hit = _cache.get(key)
    if hit is not None and (time.monotonic() - hit[0]) < _TTL_SECONDS:
        return hit[1]
    try:
        async with api_client(request) as client:
            resp = await client.get(f"/api/v1/jurisdictions/{key}/presentation")
        if resp.is_success:
            body = resp.json()["presentation"]
            pi = body.get("primary_identifier") or {}
            bank = (body.get("bank") or {}).get("fields") or []
            tx = body.get("tax") or {}
            pres = JurisdictionPresentation(
                Identifier(
                    scheme=pi.get("scheme", "generic_business_id"),
                    label=pi.get("label", _NEUTRAL_LABEL),
                    format_hint=pi.get("format_hint", ""),
                    optional=bool(pi.get("optional", False)),
                ),
                bank_fields=tuple(
                    BankField(
                        key=f["key"],
                        label=f.get("label", f["key"]),
                        format_hint=f.get("format_hint", ""),
                        optional=bool(f.get("optional", False)),
                    )
                    for f in bank
                ),
                tax=Tax(
                    term=tx.get("term", "Tax"),
                    return_name=tx.get("return_name", "Tax return"),
                    registration_term=tx.get("registration_term", "Tax registration"),
                ),
            )
            _cache[key] = (time.monotonic(), pres)
            return pres
    except Exception:  # transport/shape error — degrade, never crash a page
        _log.warning("jurisdiction presentation fetch failed for %s", key, exc_info=True)
    return _NEUTRAL


def invalidate_cache() -> None:
    _cache.clear()
