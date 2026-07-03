"""Tests for the internal LaTeX/PDF rendering service (engine #31/#32).

Covers the contract the accounting engine's new client depends on:

* template-name whitelist            → 400 on unknown / traversal
* token gate (X-Render-Token)        → 401 on wrong/missing when RENDER_TOKEN set
* happy path                         → 200 application/pdf, and the .tex source
                                       POSTed to latex-api carries latex-escaped
                                       ctx values (proves the escaping survived
                                       the port) plus PDF bytes round-trip
* latex-api 422                      → 422 {"detail": ..., "log_tail": ...}
* latex-api connection error         → 503 {"detail": "latex service unavailable"}
* pure-Jinja smoke render of document.tex.j2 with a rich Tax-Invoice ctx.

The latex-api client is two-step (POST /compile → {"pdf_url"} → GET pdf_url),
ported verbatim from the engine, so the happy-path / 422 mocks target /compile.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks_web import render
from saebooks_web.config import settings
from saebooks_web.main import app

_API_BASE = settings.latex_api_url.rstrip("/")
_FAKE_PDF = b"%PDF-1.5 fake-rendered-pdf-bytes\n%%EOF"

# A ctx rich enough for document.tex.j2 to render a full Tax Invoice, seeded
# with the four dangerous characters (&, %, _, backslash) in a field that is
# emitted through the |latex_escape filter.
_SPECIAL = "R&D 50% _pure_ C:\\path"


def _invoice_ctx() -> dict:
    return {
        "kind": "Tax Invoice",
        "number": "INV-000042",
        "issue_date": "2026-07-03",
        "due_date": "2026-07-17",
        "currency": "AUD",
        "subtotal": "100.00",
        "tax_total": "10.00",
        "total": "110.00",
        "amount_paid": "0.00",
        "payment_terms": "Net 14 days",
        "notes": _SPECIAL,
        "company": {
            "name": _SPECIAL,
            "abn": "87 744 586 592",
            "phone": "07 4243 3488",
            "email": "admin@saee.com.au",
            "website": "saee.com.au",
            "address": {
                "address_line1": "PO Box 592",
                "city": "Bungalow",
                "state": "QLD",
                "postcode": "4870",
                "country": "AU",
            },
            "bank": {
                "name": "Westpac",
                "bsb": "034-193",
                "account_number": "485846",
                "account_name": "Sauer Pty Ltd",
            },
        },
        "bank_details": {
            "name": "Westpac",
            "bsb": "034-193",
            "account_number": "485846",
            "account_name": "Sauer Pty Ltd",
        },
        "contact": {
            "name": "Acme Pty Ltd",
            "email": "ap@acme.example",
            "phone": "0400 000 000",
            "address_line1": "1 Main St",
            "city": "Cairns",
            "state": "QLD",
            "postcode": "4870",
            "country": "AU",
        },
        "lines": [
            {
                "line_no": 1,
                "description": "Fabrication",
                "quantity": "1",
                "unit_price": "100.00",
                "line_total": "100.00",
                "line_tax": "10.00",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_template_400() -> None:
    """A template name outside the six-name whitelist → 400 (path-traversal safe)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # A traversal attempt never reaches the handler: {template} is a single
        # path segment, so a slashed value fails to route (404). Either way it
        # is rejected, never rendered.
        r = await client.post("/internal/render/foo%2Fbar", json={})
        assert r.status_code in (400, 404), r.text
        # A plain non-whitelisted single-segment name → 400 from the whitelist.
        r2 = await client.post("/internal/render/not_a_template", json={})
        assert r2.status_code == 400, r2.text
        assert r2.json()["detail"] == "unknown template"


# ---------------------------------------------------------------------------
# Token gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_gate_missing_and_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    """When RENDER_TOKEN is set, a missing or wrong X-Render-Token → 401."""
    monkeypatch.setattr(render.settings, "render_token", "s3cret-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Missing header.
        r = await client.post("/internal/render/document", json=_invoice_ctx())
        assert r.status_code == 401, r.text
        # Wrong header.
        r2 = await client.post(
            "/internal/render/document",
            json=_invoice_ctx(),
            headers={"X-Render-Token": "wrong"},
        )
        assert r2.status_code == 401, r2.text


@pytest.mark.asyncio
async def test_token_absent_allows_dev_mode() -> None:
    """Empty RENDER_TOKEN (default) → endpoint open; reaches the whitelist check."""
    # No monkeypatch → settings.render_token == "" (dev). Unknown template still
    # 400, proving we got past the token gate rather than 401.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/internal/render/nope", json={})
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Happy path — escaping survives + PDF bytes round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_happy_path_escaping_and_pdf_roundtrip(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 PDF bytes; POSTed .tex carries latex-escaped ctx; correct token accepted."""
    monkeypatch.setattr(render.settings, "render_token", "good-token")

    compile_route = respx_mock.post(f"{_API_BASE}/compile").mock(
        return_value=Response(200, json={"pdf_url": "/files/out.pdf"})
    )
    respx_mock.get(f"{_API_BASE}/files/out.pdf").mock(
        return_value=Response(
            200, content=_FAKE_PDF, headers={"content-type": "application/pdf"}
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/internal/render/document",
            json=_invoice_ctx(),
            headers={"X-Render-Token": "good-token"},
        )

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == _FAKE_PDF  # PDF bytes round-trip

    # The .tex source POSTed to latex-api must carry the escaped forms.
    assert compile_route.called
    posted = json.loads(compile_route.calls.last.request.content)
    tex = posted["latex"]
    assert r"\&" in tex  # & escaped
    assert r"\%" in tex  # % escaped
    assert r"\_" in tex  # _ escaped
    assert r"\textbackslash{}" in tex  # backslash escaped
    # The raw, unescaped dangerous string must NOT appear verbatim.
    assert _SPECIAL not in tex


# ---------------------------------------------------------------------------
# latex-api 422 → 422 with log_tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_compile_error_maps_to_422(respx_mock: respx.MockRouter) -> None:
    """latex-api /compile 422 → route 422 with the log tail preserved."""
    log = "! Undefined control sequence.\nl.42 \\bogus\n                     macro"
    respx_mock.post(f"{_API_BASE}/compile").mock(
        return_value=Response(422, json={"detail": log})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/internal/render/document", json=_invoice_ctx())

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["detail"] == "latex compile failed"
    assert body["log_tail"] == log


# ---------------------------------------------------------------------------
# latex-api unreachable → 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_service_unreachable_maps_to_503(respx_mock: respx.MockRouter) -> None:
    """Connection error talking to latex-api → 503 service unavailable."""
    respx_mock.post(f"{_API_BASE}/compile").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/internal/render/document", json=_invoice_ctx())

    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "latex service unavailable"


# ---------------------------------------------------------------------------
# Pure-Jinja smoke render of document.tex.j2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_template_smoke_render() -> None:
    """Rendering document.tex.j2 with a rich Tax-Invoice ctx yields the
    letterhead + How-to-Pay panel with the bank fields filled in."""
    env = render.get_env()
    ctx = _invoice_ctx()
    ctx["company"]["name"] = "Sauer Pty Ltd"  # plain name for letterhead assert
    tex = env.get_template("document.tex.j2").render(**ctx)

    # Letterhead / From block.
    assert "Sauer Pty Ltd" in tex
    assert "ABN 87 744 586 592" in tex
    assert "admin@saee.com.au" in tex
    assert "saee.com.au" in tex
    # How-to-Pay panel (Tax Invoice only).
    assert "How to Pay" in tex
    assert "BSB:" in tex
    assert "034-193" in tex
    assert "Account Number:" in tex
    assert "485846" in tex
    assert "Sauer Pty Ltd" in tex  # account_name
    # The reference field echoes the invoice number.
    assert "INV-000042" in tex


def test_quote_phone_no_mobile_leaves_no_blank_line_in_group():
    """Regression (found live 2026-07-03): customer.phone set + mobile empty
    left whitespace-only lines inside the ESTIMATE-TO brace group; LaTeX
    reads a blank line as \\par and the group's closing ``\\\\`` then fails
    with "There's no line here to end". Assert the killer pattern is gone
    for every branch combination.
    """
    import itertools
    import re

    from saebooks_web.render import get_env

    env = get_env()
    tmpl = env.get_template("quote.tex.j2")
    killer = re.compile(r"\n[ \t]*\n[ \t]*\}?\\\\")
    for phone, mobile, contact, email in itertools.product(["", "07 4243 3488"], ["", "0457 704 373"], ["", "Bob"], ["", "x@y.z"]):
        tex = tmpl.render(
            number="1025", title="T", scope="S", issue_date="2026-07-03",
            expiry_date="", validity_days=30, deposit_pct="0",
            subtotal="1.00", total="1.10",
            customer={"name": "X", "email": email, "phone": phone, "mobile": mobile, "contact": contact},
            lines=[{"line_no": 1, "description": "d", "quantity": "1", "line_total": "1.00",
                    "section_label": None, "material": None, "length_note": None, "drawing_ref": None}],
            logo_path="",
        )
        m = killer.search(tex)
        assert not m, f"blank line before \\\\ with phone={phone!r} mobile={mobile!r} contact={contact!r} email={email!r}: ...{tex[max(0,m.start()-80):m.end()]!r}"
