"""EE (Estonian) invoice PDF — template selection + context building.

Packet 3 (feat/ee-app-surface). Companion to ``saebooks_web/render.py`` and
the ``document.tex.j2`` / ``document_ee.tex.j2`` templates.

Why this logic lives in the WEB app, not the engine
-----------------------------------------------------
``render.py``'s own module docstring: "the web app owns document
presentation." Read against a fresh checkout of the engine
(saebooks-m1, branch feat/m1-m15-global): ``saebooks/api/v1/invoices.py``'s
``_build_invoice_ctx`` / ``get_invoice_pdf`` / ``get_invoice_render_context``
hardcode ``template = "document"`` and ``kind = "Tax Invoice"`` — there is
no jurisdiction awareness on the invoice PDF path at all today (unlike
``/api/v1/tax_codes``, whose list default now resolves the company's own
jurisdiction — Packet 4a, commit e58627e). This packet is scoped to
saebooks-web and must not modify the engine, so template selection has to
happen entirely on this side: the web route fetches the engine's *facts*
(render-context + tax codes), this module decides *how to present them*,
and ``saebooks_web.render.render_latex`` does the actual render — all
in-process, no engine change required.

Data-shape gaps carried over from the engine (do not paper over these)
------------------------------------------------------------------------
* Seller registrikood (BT-30 in EN 16931 terms): ``Company`` has no
  dedicated column. The engine's own
  ``saebooks/services/einvoice/generator.py`` documents this exact gap and
  falls back to ``Company.abn`` (the one registry-code-shaped field on the
  model). This module follows the SAME convention rather than inventing a
  second one — callers pass ``ctx["company"]["abn"]`` as
  ``seller_registration_number``.
* Buyer registrikood: ``Contact.registration_number`` exists on the engine
  model (0190) but ``ContactOut`` never exposes it — confirmed by grepping
  ``saebooks/api/v1/schemas.py`` for ``registration_number`` (no hits).
  There is currently no API surface this web app can call to read it, so
  ``buyer_registration_number`` is threaded through as an optional
  parameter that the live route passes as ``None``. The template omits the
  row entirely when absent, exactly like ``document.tex.j2`` already does
  for optional ``company.abn`` — never a fabricated value. Flagged here,
  not hidden; closing it is an engine-repo change (schema + route), out of
  scope for this packet.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# The two templates render.py's TEMPLATE_NAMES whitelist knows about for
# invoices. AU (and any jurisdiction this packet hasn't built a template
# for yet) keeps the existing generic "document" template — the exact
# string the engine has always hardcoded.
_AU_TEMPLATE = "document"
_EE_TEMPLATE = "document_ee"


def select_invoice_template(jurisdiction: str | None) -> str:
    """Return the render.py template name for an invoice PDF, by jurisdiction.

    Mirrors the same "AU unless told otherwise" default the rest of this
    app's jurisdiction gating uses (see ``company_context.py`` and
    ``routes/tax_returns.py``'s ``_jurisdiction`` helper) — an unset or
    unrecognised jurisdiction falls back to the AU template, matching
    today's behaviour byte-for-byte (nothing changes for existing AU
    companies).
    """
    return _EE_TEMPLATE if (jurisdiction or "").strip().upper() == "EE" else _AU_TEMPLATE


def _to_decimal(value: Any) -> Decimal:
    """Best-effort Decimal coercion; malformed/missing input -> 0.

    Render-context values arrive as JSON (strings for money fields per
    ``_build_invoice_ctx``), so this tolerates str/int/float/Decimal/None.
    """
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _format_rate_label(rate: Decimal) -> str:
    """"24.0000" -> "24%"; "13.5000" -> "13.5%"; "0" -> "0%"."""
    text = format(rate.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def build_vat_rate_breakdown(
    lines: list[dict[str, Any]],
    tax_codes_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group invoice lines into a per-rate VAT summary (KMD box layout needs
    a taxable-amount + VAT-amount pair per rate, not the single blended
    ``tax_total`` the AU ``document.tex.j2`` totals block shows).

    Parameters
    ----------
    lines:
        ``InvoiceLineOut``-shaped dicts — reads ``tax_code_id``,
        ``line_subtotal`` (falls back to ``line_total`` if absent) and
        ``line_tax``.
    tax_codes_by_id:
        ``str(tax_code_id) -> TaxCodeOut``-shaped dict (reads ``rate`` and
        ``code``) — e.g. built from ``GET /api/v1/tax_codes`` items keyed
        by ``str(item["id"])``.

    A line whose ``tax_code_id`` is missing, or not found in
    ``tax_codes_by_id``, is grouped under rate 0 with ``unclassified=True``
    so the template can call it out explicitly rather than silently
    blending it into the real 0%-rated (export/reverse-charge) bucket.

    Returns rows sorted by rate, highest first — each a dict of
    display-ready strings: ``rate_label``, ``code``, ``taxable_amount``,
    ``tax_amount`` (2dp), plus the boolean ``unclassified``.
    """
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for line in lines:
        tax_code_id = line.get("tax_code_id")
        code_entry = tax_codes_by_id.get(str(tax_code_id)) if tax_code_id else None
        rate = _to_decimal(code_entry.get("rate")) if code_entry else Decimal("0")
        is_unclassified = code_entry is None
        key = f"{rate}|{is_unclassified}"

        if key not in buckets:
            buckets[key] = {
                "rate": rate,
                "code": (code_entry or {}).get("code") or "—",
                "taxable_amount": Decimal("0"),
                "tax_amount": Decimal("0"),
                "unclassified": is_unclassified,
            }
            order.append(key)

        bucket = buckets[key]
        taxable = line.get("line_subtotal")
        if taxable is None:
            taxable = line.get("line_total")
        bucket["taxable_amount"] += _to_decimal(taxable)
        bucket["tax_amount"] += _to_decimal(line.get("line_tax"))

    ordered_keys = sorted(
        order, key=lambda k: (buckets[k]["rate"], not buckets[k]["unclassified"]), reverse=True
    )
    return [
        {
            "rate_label": _format_rate_label(buckets[key]["rate"]),
            "code": buckets[key]["code"],
            "taxable_amount": f"{buckets[key]['taxable_amount']:.2f}",
            "tax_amount": f"{buckets[key]['tax_amount']:.2f}",
            "unclassified": buckets[key]["unclassified"],
        }
        for key in ordered_keys
    ]


def build_ee_invoice_ctx(
    base_ctx: dict[str, Any],
    *,
    vat_breakdown: list[dict[str, Any]],
    seller_registration_number: str | None,
    buyer_registration_number: str | None = None,
) -> dict[str, Any]:
    """Extend an AU-shaped render ctx with the fields ``document_ee.tex.j2``
    needs. Returns a NEW dict (shallow copy) — never mutates ``base_ctx``,
    since callers may still hold a reference to the original (e.g. for
    logging/filename purposes) after calling this.
    """
    ctx = dict(base_ctx)
    ctx["vat_breakdown"] = vat_breakdown
    ctx["seller_registration_number"] = seller_registration_number or ""
    ctx["buyer_registration_number"] = buyer_registration_number or ""
    return ctx
