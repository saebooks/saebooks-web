"""Tests for the EE invoice PDF surface — Packet 3 (feat/ee-app-surface).

``saebooks_web/invoice_pdf_ee.py`` (pure functions) + ``GET
/invoices/{id}/pdf`` (the route that wires them up).

Per the packet brief: the actual xelatex compile is only exercisable with
the real latex-api service running, which this test harness doesn't have —
those calls are mocked at the HTTP boundary exactly like
test_internal_render.py's happy path does, so what's genuinely verified
here is (1) the pure ctx-building/template-selection logic, (2) that the
route selects the right template name per jurisdiction and builds a ctx
that survives a real Jinja render (document_ee.tex.j2 smoke test), and (3)
that the correct .tex source reaches the (mocked) compile call. The actual
PDF a human would open is verified-in-demo-only, not by this suite.

Sections:
  1. Unit tests — select_invoice_template / build_vat_rate_breakdown /
     build_ee_invoice_ctx (no HTTP).
  2. Pure-Jinja smoke render of document_ee.tex.j2 (mirrors
     test_internal_render.py's document.tex.j2 smoke test).
  3. Route tests — auth guard, 404, AU happy path (document template,
     unchanged from today), EE happy path (document_ee template + VAT
     breakdown reaches the rendered .tex).
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web import render
from saebooks_web.config import settings
from saebooks_web.invoice_pdf_ee import (
    build_ee_invoice_ctx,
    build_vat_rate_breakdown,
    select_invoice_template,
)
from saebooks_web.main import app
from tests.test_jurisdiction_gating import _mock_companies, _mock_tax_codes

# ---------------------------------------------------------------------------
# 1. Unit tests — pure functions, no HTTP
# ---------------------------------------------------------------------------


def test_select_invoice_template_ee() -> None:
    assert select_invoice_template("EE") == "document_ee"


def test_select_invoice_template_lowercase_ee() -> None:
    """Case-insensitive — an "ee" jurisdiction string still selects the EE template."""
    assert select_invoice_template("ee") == "document_ee"


@pytest.mark.parametrize("jurisdiction", ["AU", "au", "", None, "US", "DE"])
def test_select_invoice_template_defaults_to_au(jurisdiction: str | None) -> None:
    """Anything that isn't EE — including unset/unrecognised — keeps the
    existing AU "document" template, matching today's behaviour."""
    assert select_invoice_template(jurisdiction) == "document"


def test_build_vat_rate_breakdown_groups_by_rate() -> None:
    tax_codes_by_id = {
        "code-24": {"id": "code-24", "code": "STD24", "rate": "24.0000"},
        "code-9": {"id": "code-9", "code": "RED9", "rate": "9.0000"},
    }
    lines = [
        {"tax_code_id": "code-24", "line_subtotal": "100.00", "line_tax": "24.00"},
        {"tax_code_id": "code-24", "line_subtotal": "50.00", "line_tax": "12.00"},
        {"tax_code_id": "code-9", "line_subtotal": "20.00", "line_tax": "1.80"},
    ]
    rows = build_vat_rate_breakdown(lines, tax_codes_by_id)

    assert rows == [
        {
            "rate_label": "24%",
            "code": "STD24",
            "taxable_amount": "150.00",
            "tax_amount": "36.00",
            "unclassified": False,
        },
        {
            "rate_label": "9%",
            "code": "RED9",
            "taxable_amount": "20.00",
            "tax_amount": "1.80",
            "unclassified": False,
        },
    ]


def test_build_vat_rate_breakdown_unclassified_line_flagged_not_folded_into_zero_rate() -> None:
    """A line with no (or unknown) tax_code_id is kept in its own bucket,
    flagged, rather than silently merged with a genuine 0%-rated line."""
    tax_codes_by_id = {
        "code-0": {"id": "code-0", "code": "EXP0", "rate": "0.0000"},
    }
    lines = [
        {"tax_code_id": "code-0", "line_subtotal": "100.00", "line_tax": "0.00"},
        {"tax_code_id": None, "line_subtotal": "30.00", "line_tax": "0.00"},
        {"line_subtotal": "5.00", "line_tax": "0.00"},  # missing key entirely
    ]
    rows = build_vat_rate_breakdown(lines, tax_codes_by_id)

    assert len(rows) == 2
    classified = next(r for r in rows if not r["unclassified"])
    unclassified = next(r for r in rows if r["unclassified"])
    assert classified["code"] == "EXP0"
    assert classified["taxable_amount"] == "100.00"
    assert unclassified["code"] == "—"
    assert unclassified["taxable_amount"] == "35.00"


def test_build_vat_rate_breakdown_empty_lines() -> None:
    assert build_vat_rate_breakdown([], {}) == []


def test_build_vat_rate_breakdown_falls_back_to_line_total_when_no_subtotal() -> None:
    tax_codes_by_id = {"c1": {"id": "c1", "code": "STD", "rate": "24"}}
    rows = build_vat_rate_breakdown(
        [{"tax_code_id": "c1", "line_total": "124.00", "line_tax": "24.00"}],
        tax_codes_by_id,
    )
    assert rows[0]["taxable_amount"] == "124.00"


def test_build_ee_invoice_ctx_does_not_mutate_base() -> None:
    base = {"number": "INV-1", "company": {"abn": "12345678"}}
    result = build_ee_invoice_ctx(
        base,
        vat_breakdown=[{"rate_label": "24%"}],
        seller_registration_number="12345678",
        buyer_registration_number=None,
    )
    assert "vat_breakdown" not in base  # original untouched
    assert result["vat_breakdown"] == [{"rate_label": "24%"}]
    assert result["seller_registration_number"] == "12345678"
    assert result["buyer_registration_number"] == ""  # None -> "" for template guard
    assert result["number"] == "INV-1"


# ---------------------------------------------------------------------------
# 2. Pure-Jinja smoke render of document_ee.tex.j2
# ---------------------------------------------------------------------------


def _ee_invoice_ctx() -> dict:
    return {
        "kind": "Tax Invoice",
        "number": "ARVE-000007",
        "issue_date": "2026-07-11",
        "due_date": "2026-07-25",
        "currency": "EUR",
        "subtotal": "170.00",
        "tax_total": "37.80",
        "total": "207.80",
        "amount_paid": "0.00",
        "payment_terms": "14 päeva",
        "notes": "",
        "seller_registration_number": "16863232",
        "buyer_registration_number": "",
        "company": {
            "name": "Sauer OU",
            "abn": "16863232",
            "phone": "+372 5555 5555",
            "email": "admin@saee.com.au",
            "website": "saee.com.au",
            "address": {
                "address_line1": "Narva mnt 5",
                "city": "Tallinn",
                "state": "",
                "postcode": "10117",
                "country": "EE",
            },
            "bank": {
                "name": "LHV Pank",
                "bsb": "",
                "account_number": "EE471000001020145685",
                "account_name": "Sauer OU",
            },
        },
        "contact": {
            "name": "Acme OU",
            "email": "ap@acme.example",
            "phone": "",
            "address_line1": "",
            "city": "",
            "state": "",
            "postcode": "",
            "country": "EE",
        },
        "lines": [
            {
                "line_no": 1,
                "description": "Konsultatsioon",
                "quantity": "1",
                "unit_price": "150.00",
                "line_total": "150.00",
                "line_tax": "24.00",
            },
            {
                "line_no": 2,
                "description": "Raamat",
                "quantity": "2",
                "unit_price": "10.00",
                "line_total": "20.00",
                "line_tax": "1.80",
            },
        ],
        "vat_breakdown": [
            {"rate_label": "24%", "code": "STD24", "taxable_amount": "150.00", "tax_amount": "24.00", "unclassified": False},
            {"rate_label": "9%", "code": "RED9", "taxable_amount": "20.00", "tax_amount": "1.80", "unclassified": False},
        ],
    }


@pytest.mark.asyncio
async def test_document_ee_template_smoke_render() -> None:
    """Rendering document_ee.tex.j2 yields Estonian labels, the registrikood
    line, the IBAN How-to-Pay panel and the per-rate VAT breakdown table."""
    env = render.get_env()
    ctx = _ee_invoice_ctx()
    tex = env.get_template("document_ee.tex.j2").render(**ctx)

    # Header / seller block.
    assert "Arve" in tex
    assert "Sauer OU" in tex
    assert "Registrikood 16863232" in tex
    assert "Tallinn" in tex
    # Line items + Estonian column headers.
    assert "Kirjeldus" in tex
    assert "Käibemaks" in tex
    assert "Konsultatsioon" in tex
    # VAT breakdown by rate (rate_label goes through |latex_escape, so the
    # literal "%" comes out as the LaTeX-escaped "\%").
    assert "Käibemaksu jaotus määrade kaupa" in tex
    assert r"24\%" in tex
    assert r"9\%" in tex
    # Maksmine (how-to-pay) panel — IBAN, no BSB row.
    assert "Maksmine" in tex
    assert "Kontonumber (IBAN)" in tex
    assert "EE471000001020145685" in tex
    assert "BSB:" not in tex  # no BSB row (only a template-comment mention of the concept)
    # Buyer registrikood omitted (blank in ctx) — guard held.
    assert tex.count("Registrikood") == 1
    assert "ARVE-000007" in tex


@pytest.mark.asyncio
async def test_document_ee_template_omits_maksmine_panel_without_bank_details() -> None:
    ctx = _ee_invoice_ctx()
    ctx["company"]["bank"] = {"name": "", "bsb": "", "account_number": "", "account_name": ""}
    env = render.get_env()
    tex = env.get_template("document_ee.tex.j2").render(**ctx)
    assert "Maksmine" not in tex


# ---------------------------------------------------------------------------
# 3. Route tests
# ---------------------------------------------------------------------------

_INV_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_API_BASE = settings.api_url.rstrip("/")
_LATEX_API_BASE = settings.latex_api_url.rstrip("/")
_FAKE_PDF = b"%PDF-1.5 fake-invoice-pdf"

_AU_COMPANY = {
    "id": "a0000000-0000-0000-0000-00000000000a",
    "name": "Acme Pty Ltd",
    "trading_name": "Acme",
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}
_EE_COMPANY = {
    "id": "e0000000-0000-0000-0000-00000000000e",
    "name": "Acme OU",
    "trading_name": "Acme OU",
    "created_at": "2026-01-01T00:00:00Z",
    "archived_at": None,
}

_TAX_CODE_ID = "aaaaaaaa-0000-0000-0000-000000000001"  # matches _mock_tax_codes' fixture id


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-invoice-pdf"})


def _mock_render_context(respx_mock: respx.MockRouter, *, currency: str = "AUD") -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}/render-context").mock(
        return_value=Response(
            200,
            json={
                "template": "document",
                "kind": "Tax Invoice",
                "ctx": {
                    "number": "INV-000099",
                    "issue_date": "2026-07-11",
                    "due_date": "2026-07-25",
                    "currency": currency,
                    "subtotal": "100.00",
                    "tax_total": "24.00",
                    "total": "124.00",
                    "amount_paid": "0.00",
                    "notes": "",
                    "payment_terms": "",
                    "company": {
                        "name": "Sauer OU" if currency == "EUR" else "Sauer Pty Ltd",
                        "abn": "16863232",
                        "phone": "",
                        "email": "admin@saee.com.au",
                        "website": "",
                        "address": {},
                        "bank": {},
                    },
                    "bank_details": {},
                    "contact": {"name": "Buyer Co"},
                    "lines": [
                        {
                            "line_no": 1,
                            "description": "Widget",
                            "quantity": "1",
                            "unit_price": "100.00",
                            "line_total": "100.00",
                            "line_tax": "24.00",
                        }
                    ],
                },
            },
        )
    )


def _mock_invoice_detail(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": _INV_ID,
                "lines": [
                    {
                        "line_no": 1,
                        "tax_code_id": _TAX_CODE_ID,
                        "line_subtotal": "100.00",
                        "line_tax": "24.00",
                        "line_total": "124.00",
                    }
                ],
            },
        )
    )


def _mock_latex_compile(respx_mock: respx.MockRouter) -> respx.Route:
    compile_route = respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(200, json={"pdf_url": "/files/inv.pdf"})
    )
    respx_mock.get(f"{_LATEX_API_BASE}/files/inv.pdf").mock(
        return_value=Response(200, content=_FAKE_PDF, headers={"content-type": "application/pdf"})
    )
    return compile_route


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False)


@pytest.mark.asyncio
async def test_invoice_pdf_auth_required() -> None:
    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/pdf")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_404(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, None)
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}/render-context").mock(
        return_value=Response(404, json={"detail": "Invoice not found"})
    )
    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/pdf", cookies={settings.session_cookie_name: _SESSION_COOKIE})
    assert resp.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_au_company_uses_document_template(respx_mock: respx.MockRouter) -> None:
    """AU company (today's only wired jurisdiction) -> unchanged behaviour:
    the generic "document" template, no VAT-breakdown fetch."""
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, "AU")
    _mock_render_context(respx_mock, currency="AUD")
    compile_route = _mock_latex_compile(respx_mock)

    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/pdf", cookies={settings.session_cookie_name: _SESSION_COOKIE})

    assert resp.status_code == 200, resp.text
    assert resp.content == _FAKE_PDF
    posted = _json.loads(compile_route.calls.last.request.content)
    tex = posted["latex"]
    assert "Tax Invoice" in tex  # AU document.tex.j2 header
    assert "Arve" not in tex  # EE template not used


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_company_uses_document_ee_template_with_vat_breakdown(
    respx_mock: respx.MockRouter,
) -> None:
    """EE company -> the document_ee template, enriched with a per-rate
    VAT breakdown built from the invoice's own lines + tax codes."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")  # id=_TAX_CODE_ID, rate="10.000"
    _mock_render_context(respx_mock, currency="EUR")
    _mock_invoice_detail(respx_mock)
    compile_route = _mock_latex_compile(respx_mock)

    async with _client() as client:
        resp = await client.get(
            f"/invoices/{_INV_ID}/pdf",
            cookies={settings.session_cookie_name: _SESSION_COOKIE},
            headers={"X-Company-Id": _EE_COMPANY["id"]},
        )

    assert resp.status_code == 200, resp.text
    assert resp.content == _FAKE_PDF
    posted = _json.loads(compile_route.calls.last.request.content)
    tex = posted["latex"]
    assert "Arve" in tex  # EE template header
    assert "Käibemaksu jaotus määrade kaupa" in tex  # VAT breakdown table
    assert r"10\%" in tex  # rate from the mocked tax code (rate="10.000"), latex-escaped
    assert "Registrikood 16863232" in tex  # seller registrikood via company.abn fallback


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_company_with_stale_au_tax_codes_misresolves_to_au_template(
    respx_mock: respx.MockRouter,
) -> None:
    """Known blind spot, pinned rather than left untested (critic round 2):
    a real EE company whose tax_codes rows are all still legacy/stale-
    tagged 'AU' (or simply empty) makes the middleware's tax_codes-based
    jurisdiction probe return zero items for that company's own
    jurisdiction. company_context.py's 'ambiguous' branch then defaults
    request.state.active_company_jurisdiction to 'AU' (logging a
    server-side warning) because CompanyOut doesn't expose
    Company.jurisdiction directly and the engine (feat/m1-m15-global) has
    no other endpoint that does either — there is no fix available on the
    web-app side alone. The consequence: this EE company's invoice PDF
    silently renders the AU 'document' template (English wording, no VAT
    breakdown, no registrikood) as a 200 success, not an error. This test
    exists so that behaviour is visible and intentional, not an untested
    gap — if the engine ever exposes Company.jurisdiction on CompanyOut
    and company_context.py is updated to use it, this test should start
    failing and needs to be flipped to assert the EE template instead."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, None)  # no tax_codes rows tagged 'EE' -> ambiguous -> "AU"
    _mock_render_context(respx_mock, currency="AUD")
    compile_route = _mock_latex_compile(respx_mock)

    async with _client() as client:
        resp = await client.get(
            f"/invoices/{_INV_ID}/pdf",
            cookies={settings.session_cookie_name: _SESSION_COOKIE},
            headers={"X-Company-Id": _EE_COMPANY["id"]},
        )

    assert resp.status_code == 200, resp.text
    posted = _json.loads(compile_route.calls.last.request.content)
    tex = posted["latex"]
    assert "Arve" not in tex  # EE template NOT used, despite this being an EE company
    assert "Käibemaksu jaotus määrade kaupa" not in tex


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_compile_error_maps_to_422(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    _mock_render_context(respx_mock, currency="EUR")
    _mock_invoice_detail(respx_mock)
    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(422, json={"detail": "! Undefined control sequence."})
    )

    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/pdf", cookies={settings.session_cookie_name: _SESSION_COOKIE})

    assert resp.status_code == 422


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_invoice_detail_failure_surfaces_error(
    respx_mock: respx.MockRouter,
) -> None:
    """If the secondary invoice-detail fetch (lines, for the VAT breakdown)
    fails, the route must not silently render a 200 PDF missing its
    statutory VAT-breakdown table — it should surface the upstream failure."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    _mock_render_context(respx_mock, currency="EUR")
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}").mock(
        return_value=Response(500, json={"detail": "boom"})
    )

    async with _client() as client:
        resp = await client.get(
            f"/invoices/{_INV_ID}/pdf",
            cookies={settings.session_cookie_name: _SESSION_COOKIE},
            headers={"X-Company-Id": _EE_COMPANY["id"]},
        )

    assert resp.status_code == 500
    assert "VAT breakdown" in resp.text


# ---------------------------------------------------------------------------
# 4. Critic round 3 — archived tax code + internal-error-detail leaks
# ---------------------------------------------------------------------------

_ARCHIVED_TAX_CODE_ID = "aaaaaaaa-0000-0000-0000-000000000099"


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_resolves_archived_tax_code_by_id(
    respx_mock: respx.MockRouter,
) -> None:
    """A line's tax code has since been archived, so it's absent from
    GET /api/v1/tax_codes' list (engine filters archived_at IS NULL) —
    the route must fall back to GET /api/v1/tax_codes/{id} (no archived
    filter) rather than silently bucketing the line as unclassified/0%
    on a statutory VAT document."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")  # list -> only the live code, id=_TAX_CODE_ID
    _mock_render_context(respx_mock, currency="EUR")
    respx_mock.get(f"{_API_BASE}/api/v1/invoices/{_INV_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": _INV_ID,
                "lines": [
                    {
                        "line_no": 1,
                        "tax_code_id": _ARCHIVED_TAX_CODE_ID,
                        "line_subtotal": "100.00",
                        "line_tax": "24.00",
                        "line_total": "124.00",
                    }
                ],
            },
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes/{_ARCHIVED_TAX_CODE_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": _ARCHIVED_TAX_CODE_ID,
                "code": "STD24",
                "rate": "24.000",
                "archived_at": "2026-06-01T00:00:00Z",
            },
        )
    )
    compile_route = _mock_latex_compile(respx_mock)

    async with _client() as client:
        resp = await client.get(
            f"/invoices/{_INV_ID}/pdf",
            cookies={settings.session_cookie_name: _SESSION_COOKIE},
            headers={"X-Company-Id": _EE_COMPANY["id"]},
        )

    assert resp.status_code == 200, resp.text
    posted = _json.loads(compile_route.calls.last.request.content)
    tex = posted["latex"]
    assert r"24\%" in tex  # resolved from the archived-code lookup, not "unclassified"


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_compile_error_hides_log_tail(
    respx_mock: respx.MockRouter,
) -> None:
    """The 422 latex-compile-failure response must not leak the raw
    xelatex log tail (internal container paths/template internals) to the
    browser — generic message only."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    _mock_render_context(respx_mock, currency="EUR")
    _mock_invoice_detail(respx_mock)
    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(
            422, json={"detail": "! Undefined control sequence. /srv/latex/job123.log:42: ..."}
        )
    )

    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/pdf", cookies={settings.session_cookie_name: _SESSION_COOKIE})

    assert resp.status_code == 422
    assert "job123" not in resp.text
    assert "/srv/latex" not in resp.text
    assert resp.json() == {"detail": "latex compile failed"}


@pytest.mark.asyncio
@respx.mock
async def test_invoice_pdf_ee_service_unavailable_hides_internal_url(
    respx_mock: respx.MockRouter,
) -> None:
    """The 503 latex-service-unavailable response must not leak the
    internal latex-api URL/exception text to the browser — generic
    message only, matching render.py's own internal/render pattern for
    this exception class."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    _mock_render_context(respx_mock, currency="EUR")
    _mock_invoice_detail(respx_mock)
    import httpx as _httpx

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(side_effect=_httpx.ConnectError("refused"))

    async with _client() as client:
        resp = await client.get(f"/invoices/{_INV_ID}/pdf", cookies={settings.session_cookie_name: _SESSION_COOKIE})

    assert resp.status_code == 503
    assert "latex-api" not in resp.text
    assert resp.json() == {"detail": "latex service unavailable"}
