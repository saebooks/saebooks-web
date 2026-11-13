"""Batch 7 screens -- reports that agree with the ledger.

P1-26 general ledger / journal / accountant pack, P1-27 backup and export,
P1-28 report PDFs, P1-29 period columns, P1-33 budget subtotals, P1-35
reports index, P0-22 cash flow lines.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.config import settings
from saebooks_web.main import app


def _cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    return signer.sign(_b64encode(_json.dumps(data).encode("utf-8"))).decode("utf-8")


_USER = _cookie({"api_token": "t", "user_role": "bookkeeper"})
_ADMIN = _cookie({"api_token": "t", "user_role": "admin"})
_API = settings.api_url.rstrip("/")


def _client(cookie: str = _USER) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: cookie},
    )


# ---------------------------------------------------------------------------
# P1-28 -- the report PDF template is allowed and renders the table kind
# ---------------------------------------------------------------------------


def test_report_single_is_an_allowed_template() -> None:
    from saebooks_web.render import TEMPLATE_NAMES

    assert "report_single" in TEMPLATE_NAMES


def test_report_single_table_kind_renders_latex() -> None:
    from saebooks_web.render import get_env

    tex = get_env().get_template("report_single.tex.j2").render(
        kind="table",
        company={"name": "Acme & Sons", "legal_name": "", "acn": "", "abn": "", "identifier_label": "ABN"},
        title="Cash Flow Statement",
        period_label="For the period 2026-07-01 to 2026-09-30",
        prepared="2026-11-13",
        columns=[{"label": "", "align": "X"}, {"label": "Amount", "align": "r"}],
        rows=[
            {"cells": ["Operating activities", ""], "style": "heading"},
            {"cells": ["Increase in Trade Debtors", "(500.00)"], "style": None},
            {"cells": ["Net change in cash", "(500.00)"], "style": "total"},
        ],
        notes=["Cash means: 1-1114 Bank 100%"],
        logo_path=None,
    )
    body = tex[tex.index(r"\begin{longtable}") : tex.index(r"\end{longtable}")]
    assert r"\begin{longtable}{>{\raggedright\arraybackslash}p{13.6cm}r}" in body
    assert "Increase in Trade Debtors & (500.00)" in body
    assert r"\midrule" in body
    assert "Acme \\& Sons" in tex
    assert "Bank 100\\%" in tex
    # No blank line between rows (a blank line before \midrule breaks LaTeX).
    rows = body[body.index(r"\endfoot") :]
    assert "\n\n" not in rows


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,engine",
    [
        ("aged_receivables.pdf", "aged_receivables.pdf"),
        ("aged_payables.pdf", "aged_payables.pdf"),
        ("cashflow.pdf", "cashflow.pdf"),
        ("bas_summary.pdf", "bas_summary.pdf"),
        ("general_ledger.csv", "general_ledger.csv"),
        ("journal.xlsx", "journal.xlsx"),
        ("profit_loss_columns.csv", "profit_loss_columns.csv"),
        ("fixed_asset_register.xlsx", "fixed_asset_register.xlsx"),
        ("accountant_pack.zip", "accountant_pack.zip"),
    ],
)
async def test_new_exports_are_proxied(respx_mock: respx.MockRouter, name: str, engine: str) -> None:
    route = respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/{engine}.*$").mock(
        return_value=Response(200, content=b"data", headers={"content-disposition": f'attachment; filename="{engine}"'})
    )
    async with _client() as c:
        resp = await c.get(f"/reports/export/{name}?from_date=2026-07-01&to_date=2026-09-30")
    assert resp.status_code == 200
    assert resp.content == b"data"
    assert route.called


# ---------------------------------------------------------------------------
# P1-35 -- reports index
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_reports_index_has_no_placeholder_text(respx_mock: respx.MockRouter) -> None:
    from tests import _jp

    _jp.mock_au_context(respx_mock)
    async with _client() as c:
        resp = await c.get("/reports")
    assert resp.status_code == 200
    text = resp.text
    assert "Tier 2" not in text
    assert "budget-vs-actual?export=csv" not in text
    for href in (
        "/reports/general-ledger",
        "/reports/journal",
        "/reports/accountant-pack",
        "/reports/data-export",
        "/reports/periods?report=profit_loss",
    ):
        assert href in text
    # No anchor nested inside the Budget card's anchor.
    card = text[text.index('href="/reports/budget-vs-actual"') :]
    card = card[: card.index("</a>")]
    assert "<a " not in card


# ---------------------------------------------------------------------------
# P1-26 -- general ledger, journal, accountant pack
# ---------------------------------------------------------------------------

_GL = {
    "from_date": "2026-07-01",
    "to_date": "2026-09-30",
    "accounts": [
        {
            "account_id": "a1",
            "code": "1-1114",
            "name": "Westpac Business One",
            "account_type": "ASSET",
            "balance_side": "debit",
            "opening": 10000.0,
            "total_debit": 600.0,
            "total_credit": 3000.0,
            "closing": 7600.0,
            "lines": [
                {"entry_id": "e1", "date": "2026-07-20", "ref": "JE-000101", "source": "Receipt",
                 "description": "Smith pays INV-1", "debit": 600.0, "credit": 0.0, "balance": 10600.0},
                {"entry_id": "e2", "date": "2026-08-10", "ref": "JE-000102", "source": "Bill",
                 "description": "Welder", "debit": 0.0, "credit": 3000.0, "balance": 7600.0},
            ],
        }
    ],
    "total_debit": 600.0,
    "total_credit": 3000.0,
}


@pytest.mark.anyio
@respx.mock
async def test_general_ledger_page(respx_mock: respx.MockRouter) -> None:
    gl = respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/general_ledger.*$").mock(
        return_value=Response(200, json=_GL)
    )
    respx_mock.get(url__regex=rf"^{_API}/api/v1/accounts.*$").mock(
        return_value=Response(200, json={"items": [{"id": "a1", "code": "1-1114", "name": "Westpac Business One"}]})
    )
    async with _client() as c:
        resp = await c.get("/reports/general-ledger?from_date=2026-07-01&to_date=2026-09-30")
    assert resp.status_code == 200
    text = resp.text
    assert "General Ledger" in text
    assert "Opening balance" in text and "Closing balance" in text
    assert "10600.00" in text and "7600.00" in text
    assert "Smith pays INV-1" in text
    assert "/reports/export/general_ledger.pdf?" in text
    assert "from_date=2026-07-01" in str(gl.calls[0].request.url)


@pytest.mark.anyio
@respx.mock
async def test_general_ledger_htmx_partial(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/general_ledger.*$").mock(
        return_value=Response(200, json=_GL)
    )
    async with _client() as c:
        resp = await c.get(
            "/reports/general-ledger?from_date=2026-07-01&to_date=2026-09-30",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "report-content" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_journal_page_pages(respx_mock: respx.MockRouter) -> None:
    body = {
        "from_date": "2026-07-01",
        "to_date": "2026-09-30",
        "total": 250,
        "limit": 100,
        "offset": 100,
        "entries": [
            {
                "entry_id": "e1", "date": "2026-07-05", "ref": "JE-000201", "source": "Invoice",
                "status": "POSTED", "description": "INV-000090",
                "lines": [
                    {"account_code": "1-1200", "account_name": "Trade Debtors", "description": "", "debit": 1100.0, "credit": 0.0},
                    {"account_code": "4-1000", "account_name": "Sales", "description": "", "debit": 0.0, "credit": 1000.0},
                ],
            }
        ],
    }
    route = respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/journal.*$").mock(
        return_value=Response(200, json=body)
    )
    async with _client() as c:
        resp = await c.get("/reports/journal?from_date=2026-07-01&to_date=2026-09-30&page=2")
    assert resp.status_code == 200
    assert "JE-000201" in resp.text and "Trade Debtors" in resp.text
    assert "Page 2 of 3" in resp.text
    assert "offset=100" in str(route.calls[0].request.url)


@pytest.mark.anyio
@respx.mock
async def test_accountant_pack_page_defaults_to_last_financial_year(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/companies.*$").mock(
        return_value=Response(200, json={"items": [{"fin_year_start_month": 7}]})
    )
    async with _client() as c:
        resp = await c.get("/reports/accountant-pack")
    assert resp.status_code == 200
    assert "/reports/export/accountant_pack.zip?from_date=" in resp.text
    assert "-07-01" in resp.text and "-06-30" in resp.text


# ---------------------------------------------------------------------------
# P1-29 -- period columns
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_periods_page_shows_columns_and_change(respx_mock: respx.MockRouter) -> None:
    data = {
        "report": "profit_loss",
        "period": "month",
        "from_date": "2026-07-01",
        "to_date": "2026-08-31",
        "columns": [
            {"label": "Jul 2026", "from_date": "2026-07-01", "to_date": "2026-07-31"},
            {"label": "Aug 2026", "from_date": "2026-08-01", "to_date": "2026-08-31"},
        ],
        "lines": [
            {"account_id": "i", "code": "4-1000", "account_name": "Sales", "section": "income",
             "amounts": [1000.0, 1500.0], "total": 2500.0, "change": 500.0, "change_pct": 50.0},
            {"account_id": "x", "code": "6-1000", "account_name": "Materials", "section": "expense",
             "amounts": [400.0, 0.0], "total": 400.0, "change": -400.0, "change_pct": -100.0},
        ],
        "totals": [
            {"label": "Total income", "amounts": [1000.0, 1500.0], "total": 2500.0, "change": 500.0, "change_pct": 50.0},
            {"label": "Total expenses", "amounts": [400.0, 0.0], "total": 400.0, "change": -400.0, "change_pct": -100.0},
            {"label": "Net profit", "amounts": [600.0, 1500.0], "total": 2100.0, "change": 900.0, "change_pct": 150.0},
        ],
    }
    route = respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/profit_loss_columns.*$").mock(
        return_value=Response(200, json=data)
    )
    async with _client() as c:
        resp = await c.get("/reports/periods?report=profit_loss&period=month&from_date=2026-07-01&to_date=2026-08-31")
    assert resp.status_code == 200
    text = resp.text
    assert "Jul 2026" in text and "Aug 2026" in text
    assert "Change" in text and "150.0%" in text
    assert "Net profit" in text and "2100.00" in text
    assert "period=month" in str(route.calls[0].request.url)


@pytest.mark.anyio
@respx.mock
async def test_periods_page_reports_too_many_columns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/balance_sheet_columns.*$").mock(
        return_value=Response(422, json={"detail": "That range makes more than 24 columns."})
    )
    async with _client() as c:
        resp = await c.get("/reports/periods?report=balance_sheet&period=month&from_date=2020-01-01&to_date=2026-08-31")
    assert resp.status_code == 200
    assert "more than 24 columns" in resp.text


# ---------------------------------------------------------------------------
# P1-27 -- backup and export
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_data_export_page_non_admin_cannot_download() -> None:
    async with _client(_USER) as c:
        page = await c.get("/reports/data-export")
        dl = await c.get("/reports/data-export/download")
    assert page.status_code == 200
    assert "Only an administrator" in page.text
    assert 'href="/reports/data-export/download"' not in page.text
    assert dl.status_code == 303


@pytest.mark.anyio
@respx.mock
async def test_data_export_admin_download(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/data_export\.zip$").mock(
        return_value=Response(200, content=b"PK\x03\x04zip", headers={"content-disposition": 'attachment; filename="company-x.zip"'})
    )
    async with _client(_ADMIN) as c:
        page = await c.get("/reports/data-export")
        dl = await c.get("/reports/data-export/download")
    assert 'href="/reports/data-export/download"' in page.text
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    assert dl.content.startswith(b"PK")


# ---------------------------------------------------------------------------
# P0-22 cash flow + P1-33 budget screens
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_cashflow_shows_account_lines_and_bank_accounts(respx_mock: respx.MockRouter) -> None:
    report = {
        "from_date": "2026-07-01",
        "to_date": "2026-09-30",
        "operating": {
            "net_profit": 470.0,
            "adjustments": [{"account_id": "d", "code": "1-2110", "name": "Accum dep", "label": "Depreciation and amortisation (Accum dep)", "amount": 50.0}],
            "working_capital": [{"account_id": "t", "code": "1-1200", "name": "Trade Debtors", "label": "Increase in Trade Debtors", "amount": -500.0}],
            "total_operating": 20.0,
        },
        "investing": {"asset_purchases": -3000.0, "asset_disposals": 0.0, "lines": [{"account_id": "p", "code": "1-2100", "name": "Plant", "label": "Purchase of Plant", "amount": -3000.0}], "total_investing": -3000.0},
        "financing": {"loan_proceeds": 2000.0, "loan_repayments": 0.0, "lines": [{"account_id": "l", "code": "2-2200", "name": "Directors Loan", "label": "Borrowed (Directors Loan)", "amount": 2000.0}], "total_financing": 2000.0},
        "net_change": -980.0,
        "opening_cash": 10000.0,
        "closing_cash": 9020.0,
        "cash_accounts": [{"account_id": "b", "code": "1-1114", "name": "Westpac Business One", "opening": 10000.0, "closing": 9020.0}],
        "reconciled": True,
        "difference": 0.0,
        "warnings": [],
    }
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/cashflow.*$").mock(return_value=Response(200, json=report))
    async with _client() as c:
        resp = await c.get("/reports/cashflow?from_date=2026-07-01&to_date=2026-09-30")
    assert resp.status_code == 200
    text = resp.text
    for s in ("Increase in Trade Debtors", "Purchase of Plant", "Borrowed (Directors Loan)", "Westpac Business One", "9020.00"):
        assert s in text
    assert "add up to the change in your bank and cash balances" in text
    assert "/reports/export/cashflow.pdf?" in text


@pytest.mark.anyio
@respx.mock
async def test_budget_shows_income_and_expense_subtotals(respx_mock: respx.MockRouter) -> None:
    report = {
        "year": 2026,
        "month": None,
        "lines": [
            {"account_id": "i", "account_code": "4-1000", "account_name": "Sales", "account_type": "INCOME", "section": "income",
             "budget": 1200.0, "actual": 1000.0, "variance": -200.0, "variance_pct": -16.7},
            {"account_id": "x", "account_code": "6-1000", "account_name": "Materials", "account_type": "EXPENSE", "section": "expense",
             "budget": 250.0, "actual": 300.0, "variance": 50.0, "variance_pct": None},
        ],
        "total_income_budget": 1200.0, "total_income_actual": 1000.0, "total_income_variance": -200.0,
        "total_expense_budget": 250.0, "total_expense_actual": 300.0, "total_expense_variance": 50.0,
        "total_budget": 950.0, "total_actual": 700.0, "total_variance": -250.0,
    }
    respx_mock.get(url__regex=rf"^{_API}/api/v1/reports/budget_vs_actual.*$").mock(return_value=Response(200, json=report))
    async with _client() as c:
        resp = await c.get("/reports/budget-vs-actual")
    assert resp.status_code == 200
    text = resp.text
    assert "Total income" in text and "Total expenses" in text and "Net profit" in text
    assert "950.00" in text and "700.00" in text
    # Expense over budget is shown as bad even though the variance is positive.
    materials = text[text.index("Materials") :]
    assert "var(--neg)" in materials[: materials.index("</tr>")]
