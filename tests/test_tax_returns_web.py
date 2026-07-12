"""Tests for the Deklaratsioonid / Tax returns web screen (Packet 2).

Thin proxy over the engine's ``/api/v1/tax_returns`` surface (Packet 4c —
734b901). Covers:

1. test_tax_returns_requires_auth        — 303 -> /login without session
2. test_tax_returns_list_renders         — list shows KMD + TSD rows, status
3. test_tax_returns_generate_success     — 201 -> flash + redirect to detail
4. test_tax_returns_generate_422_surfaced — engine's 422 detail shown verbatim
5. test_tax_returns_detail_kmd_renders   — box-vector figures table
6. test_tax_returns_detail_tsd_renders   — TSD main totals + data-quality
                                            warnings surfaced
7. test_tax_returns_mark_filed           — status transition + filed_at shown
8. test_tax_returns_mark_filed_already_filed_422 — engine 422 surfaced
9. test_tax_returns_export_download      — KMD export streams XML with the
                                            engine's filename
10. test_tax_returns_export_not_exportable_flashes — TSD 501 surfaced, not a
                                            raw error page
11. test_tax_returns_nav_hidden_for_au   — AU company: no Deklaratsioonid link
12. test_tax_returns_nav_shown_for_ee    — EE company: Deklaratsioonid link present
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

_API_BASE = settings.api_url.rstrip("/")

_KMD_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_TSD_ID = "bbbbbbbb-0000-0000-0000-000000000002"
_PERIOD_ID = "cccccccc-0000-0000-0000-000000000003"

_AU_COMPANY_ID = "a0000000-0000-0000-0000-00000000000a"
_EE_COMPANY_ID = "e0000000-0000-0000-0000-00000000000e"

_AU_COMPANY = {
    "id": _AU_COMPANY_ID, "name": "Acme Pty Ltd", "trading_name": "Acme",
    "created_at": "2026-01-01T00:00:00Z", "archived_at": None,
}
_EE_COMPANY = {
    "id": _EE_COMPANY_ID, "name": "Acme OU", "trading_name": "Acme OU",
    "created_at": "2026-01-01T00:00:00Z", "archived_at": None,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-tax-returns"})


def _mock_companies(respx_mock: respx.MockRouter, company: dict) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/companies(\?.*)?$").mock(
        return_value=Response(200, json={"items": [company], "total": 1})
    )


def _mock_tax_codes(respx_mock: respx.MockRouter, jurisdiction: str) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_codes(\?.*)?$").mock(
        return_value=Response(200, json={
            "items": [{
                "id": "dddddddd-0000-0000-0000-000000000004",
                "code": "T1", "name": "Test code", "rate": "20.000",
                "tax_system": "VAT" if jurisdiction == "EE" else "GST",
                "jurisdiction": jurisdiction,
            }],
            "total": 1,
        })
    )


def _kmd_return(**overrides) -> dict:
    base = {
        "id": _KMD_ID,
        "company_id": _EE_COMPANY_ID,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "jurisdiction": "EE",
        "period_id": _PERIOD_ID,
        "return_type": "KMD",
        "figures": {
            "1": {"amount": "1000.00", "label": "Taxable supplies 20%", "display_order": 1},
            "4": {"amount": "200.00", "label": "VAT payable", "display_order": 2},
        },
        "generated_at": "2026-07-10T00:00:00Z",
        "generated_by_user_id": None,
        "status": "ready",
        "lodgement_record_id": None,
        "filed_at": None,
    }
    base.update(overrides)
    return base


def _tsd_return(**overrides) -> dict:
    base = {
        "id": _TSD_ID,
        "company_id": _EE_COMPANY_ID,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "jurisdiction": "EE",
        "period_id": _PERIOD_ID,
        "return_type": "TSD",
        "figures": {
            "main": {
                "employee_count": 2, "total_gross": "5000.00",
                "total_income_tax": "1000.00", "total_unemployment_employee": "80.00",
                "total_unemployment_employer": "40.00", "total_social_tax": "1650.00",
                "total_pillar_ii": "150.00",
            },
            "lisa1": [{"employee_id": "e1"}, {"employee_id": "e2"}],
            "errors": [
                {"employee_id": "e3", "employee_name": "Jane Doe",
                 "pay_run_id": "pr1", "message": "No isikukood on file"},
            ],
            "gl_not_posted_pay_run_ids": [],
        },
        "generated_at": "2026-07-10T00:00:00Z",
        "generated_by_user_id": None,
        "status": "ready",
        "lodgement_record_id": None,
        "filed_at": None,
    }
    base.update(overrides)
    return base


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


# ---------------------------------------------------------------------------
# 1. Auth guard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tax_returns_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    ) as client:
        resp = await client.get("/tax-returns")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. List
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_list_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_returns(\?.*)?$").mock(
        return_value=Response(200, json={
            "items": [_kmd_return(), _tsd_return()], "total": 2,
        })
    )

    async with _client() as client:
        resp = await client.get("/tax-returns")

    assert resp.status_code == 200
    assert "Deklaratsioonid" in resp.text
    assert "KMD" in resp.text
    assert "TSD" in resp.text
    assert "Ready" in resp.text


# ---------------------------------------------------------------------------
# 3-4. Generate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_generate_success(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/tax_returns/generate").mock(
        return_value=Response(201, json={
            "id": _KMD_ID, "jurisdiction": "EE", "period_id": _PERIOD_ID,
            "return_type": "KMD", "status": "ready", "figures": {},
        })
    )

    async with _client() as client:
        resp = await client.post(
            "/tax-returns/generate",
            data={"return_type": "KMD", "period_id": _PERIOD_ID},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/tax-returns/{_KMD_ID}"


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_generate_422_surfaced(respx_mock: respx.MockRouter) -> None:
    """The engine's 422 'no box definitions' detail is shown, not swallowed
    into a generic error — TSD isn't generatable via this action."""
    respx_mock.post(f"{_API_BASE}/api/v1/tax_returns/generate").mock(
        return_value=Response(422, json={
            "detail": "No box definitions found for jurisdiction=EE return_type=TSD",
        })
    )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/tax_returns(\?.*)?$").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with _client() as client:
        gen_resp = await client.post(
            "/tax-returns/generate",
            data={"return_type": "TSD", "period_id": _PERIOD_ID},
            follow_redirects=False,
        )
        assert gen_resp.status_code == 303
        list_resp = await client.get("/tax-returns")

    assert "No box definitions found for jurisdiction=EE return_type=TSD" in list_resp.text


# ---------------------------------------------------------------------------
# 5-6. Detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_detail_kmd_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_KMD_ID}").mock(
        return_value=Response(200, json=_kmd_return())
    )

    async with _client() as client:
        resp = await client.get(f"/tax-returns/{_KMD_ID}")

    assert resp.status_code == 200
    assert "Taxable supplies 20%" in resp.text
    assert "1000.00" in resp.text
    assert "Download XML" in resp.text  # EE/KMD is exportable


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_detail_tsd_renders(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_TSD_ID}").mock(
        return_value=Response(200, json=_tsd_return())
    )

    async with _client() as client:
        resp = await client.get(f"/tax-returns/{_TSD_ID}")

    assert resp.status_code == 200
    # Main totals rendered
    assert "5000.00" in resp.text
    # Data-quality warning surfaced, not swallowed
    assert "Jane Doe" in resp.text
    assert "No isikukood on file" in resp.text
    # TSD has no wired document builder — no download link offered
    assert "Download XML" not in resp.text


# ---------------------------------------------------------------------------
# 7-8. Mark filed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_mark_filed(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/tax_returns/{_KMD_ID}/file").mock(
        return_value=Response(200, json={
            "return_id": _KMD_ID, "status": "filed", "filed_at": "2026-07-12T01:00:00Z",
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_KMD_ID}").mock(
        return_value=Response(200, json=_kmd_return(status="filed", filed_at="2026-07-12T01:00:00Z"))
    )

    async with _client() as client:
        file_resp = await client.post(
            f"/tax-returns/{_KMD_ID}/file", data={"reference": "EMTA-REC-1"},
            follow_redirects=False,
        )
        assert file_resp.status_code == 303
        detail_resp = await client.get(f"/tax-returns/{_KMD_ID}")

    assert "Filed" in detail_resp.text
    assert "2026-07-12T01:00:00Z" in detail_resp.text


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_mark_filed_already_filed_422(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/tax_returns/{_KMD_ID}/file").mock(
        return_value=Response(422, json={
            "detail": f"Tax return {_KMD_ID} is already filed",
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_KMD_ID}").mock(
        return_value=Response(200, json=_kmd_return(status="filed", filed_at="2026-07-01T00:00:00Z"))
    )

    async with _client() as client:
        file_resp = await client.post(
            f"/tax-returns/{_KMD_ID}/file", data={}, follow_redirects=False,
        )
        assert file_resp.status_code == 303
        detail_resp = await client.get(f"/tax-returns/{_KMD_ID}")

    assert "already filed" in detail_resp.text


# ---------------------------------------------------------------------------
# 9-10. Export
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_export_download(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_KMD_ID}/export").mock(
        return_value=Response(
            200,
            content=b"<KMD>...</KMD>",
            headers={
                "content-type": "application/xml",
                "content-disposition": f'attachment; filename="KMD_{_KMD_ID}.xml"',
            },
        )
    )

    async with _client() as client:
        resp = await client.get(f"/tax-returns/{_KMD_ID}/export")

    assert resp.status_code == 200
    assert resp.content == b"<KMD>...</KMD>"
    assert "application/xml" in resp.headers["content-type"]
    assert f"KMD_{_KMD_ID}.xml" in resp.headers["content-disposition"]


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_export_not_exportable_flashes(respx_mock: respx.MockRouter) -> None:
    """TSD /export 501s upstream (no document builder wired) — surfaced as
    a flash on the detail page, not a raw error page on a download link."""
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_TSD_ID}/export").mock(
        return_value=Response(501, json={
            "detail": "Export not implemented for jurisdiction='EE' return_type='TSD'.",
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_returns/{_TSD_ID}").mock(
        return_value=Response(200, json=_tsd_return())
    )

    async with _client() as client:
        export_resp = await client.get(f"/tax-returns/{_TSD_ID}/export", follow_redirects=False)
        assert export_resp.status_code == 303
        detail_resp = await client.get(f"/tax-returns/{_TSD_ID}")

    # Jinja auto-escapes the quotes in the engine's detail message.
    assert "Export not implemented for jurisdiction=&#39;EE&#39; return_type=&#39;TSD&#39;." in detail_resp.text


# ---------------------------------------------------------------------------
# 11-12. Nav gating — AU hides, EE shows
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_nav_hidden_for_au(respx_mock: respx.MockRouter) -> None:
    """An AU company sees its existing BAS/ATO-SBR rail, unchanged — no
    Deklaratsioonid link. Hits /tax-codes (not /tax-returns itself, whose
    own page heading always says "Deklaratsioonid" regardless of
    jurisdiction — this is a nav-gating check) so the only mocks needed
    are the middleware's own tax_codes probe, reused as the page's data."""
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, "AU")

    async with _client() as client:
        resp = await client.get("/tax-codes")

    assert resp.status_code == 200
    assert "Deklaratsioonid" not in resp.text
    # AU's existing equivalent is unchanged.
    assert "BAS worksheet" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_tax_returns_nav_shown_for_ee(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")

    async with _client() as client:
        resp = await client.get("/tax-codes")

    assert resp.status_code == 200
    assert "Deklaratsioonid" in resp.text
    assert "BAS worksheet" not in resp.text
