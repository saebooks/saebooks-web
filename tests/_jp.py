"""Shared test helper: register jurisdiction-presentation routes on respx.

base.html now renders nav/palette from the jurisdiction presentation contract
(request.state.jp), so any test that renders a page for an AU/EE company must
let company_context fetch a contract — otherwise it degrades to the neutral
(features-off) contract and the AU nav affordances (BAS/ATO/payroll) vanish.

Call ``mock_presentations(respx_mock)`` in such tests. Host-agnostic (url__regex),
so it works whatever base URL the test's client uses.
"""
from __future__ import annotations

import re

from httpx import Response

_AU = {
    "code": "AU",
    "presentation": {
        "primary_identifier": {"scheme": "au_abn", "label": "ABN",
                                "format_hint": "NN NNN NNN NNN", "optional": False},
        "bank": {"fields": [
            {"key": "bank_bsb", "label": "BSB", "format_hint": "062-000", "optional": False},
            {"key": "bank_account_number", "label": "Account number", "format_hint": "", "optional": False},
            {"key": "bank_account_title", "label": "Account holder name", "format_hint": "", "optional": True},
        ]},
        "tax": {"term": "GST", "return_name": "BAS", "registration_term": "GST registration"},
        "currency": {"default": "AUD"},
        "default_country": "Australia",
        "features": {"payroll": True, "tax_reports": True},
    },
}
_EE = {
    "code": "EE",
    "presentation": {
        "primary_identifier": {"scheme": "ee_regcode", "label": "Registrikood",
                                "format_hint": "NNNNNNNN", "optional": False},
        "bank": {"fields": [
            {"key": "iban", "label": "IBAN", "format_hint": "", "optional": False},
            {"key": "bic", "label": "BIC / SWIFT", "format_hint": "", "optional": True},
        ]},
        "tax": {"term": "käibemaks", "return_name": "KMD", "registration_term": "KMKR registreerimine"},
        "currency": {"default": "EUR"},
        "default_country": "Estonia",
        "features": {"payroll": False, "tax_reports": False},
    },
}


def mock_presentations(respx_mock) -> None:
    """Register AU (features on) + EE (features off) presentation routes, and
    clear the process cache so a prior test's contract can't leak in."""
    from saebooks_web.jurisdiction_presentation import invalidate_cache
    invalidate_cache()
    for code, body in (("AU", _AU), ("EE", _EE)):
        respx_mock.get(
            url__regex=rf".*/api/v1/jurisdictions/{code}/presentation.*"
        ).mock(return_value=Response(200, json=body))


def mock_au_context(respx_mock) -> None:
    """Full AU jurisdiction context for tests that render AU-specific nav
    (ATO SBR, pay-run, AU bank fields) without setting up their own company:
    companies + AU-stamped tax_codes (so company_context resolves 'AU') +
    the presentation routes. Call as the FIRST line so these register before
    the test's own narrower mocks."""
    mock_presentations(respx_mock)
    respx_mock.get(url__regex=r".*/api/v1/companies.*").mock(
        return_value=Response(200, json={"items": [
            {"id": "a0000000-0000-0000-0000-00000000000a", "name": "AU Co",
             "created_at": "2026-01-01T00:00:00Z", "bookkeeping_mode": "full"}
        ], "total": 1})
    )
    respx_mock.get(url__regex=r".*/api/v1/tax_codes.*").mock(
        return_value=Response(200, json={"items": [
            {"id": "aaaaaaaa-0000-0000-0000-000000000001", "code": "GST",
             "name": "GST", "rate": "10.000", "tax_system": "GST",
             "jurisdiction": "AU"}
        ], "total": 1})
    )


def mock_ee_context(respx_mock) -> None:
    """Full EE jurisdiction context — companies + EE-stamped tax_codes +
    presentations — so company_context resolves 'EE' and the GUI renders the
    Estonian contract (Registrikood / IBAN / käibemaks / EUR, features off)."""
    mock_presentations(respx_mock)
    respx_mock.get(url__regex=r".*/api/v1/companies.*").mock(
        return_value=Response(200, json={"items": [
            {"id": "e0000000-0000-0000-0000-00000000000e", "name": "EE Co",
             "created_at": "2026-01-01T00:00:00Z", "bookkeeping_mode": "full"}
        ], "total": 1})
    )
    respx_mock.get(url__regex=r".*/api/v1/tax_codes.*").mock(
        return_value=Response(200, json={"items": [
            {"id": "eeeeeeee-0000-0000-0000-000000000001", "code": "KM",
             "name": "Käibemaks", "rate": "22.000", "tax_system": "VAT",
             "jurisdiction": "EE"}
        ], "total": 1})
    )
