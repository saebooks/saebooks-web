"""Tests for the Imports wizard web views — Lane D cycle 54.

1.  test_imports_landing_requires_auth       — GET /admin/imports/ without session -> 303 /login
2.  test_imports_landing_renders             — GET /admin/imports/ shows three import options
3.  test_imports_bank_requires_auth          — GET /admin/imports/bank without session -> 303
4.  test_imports_bank_renders                — GET /admin/imports/bank shows upload form
5.  test_imports_bank_preview_requires_auth  — POST /admin/imports/bank/preview without session -> 303
6.  test_imports_bank_preview_success        — POST multipart -> API 200 -> preview rendered
7.  test_imports_bank_preview_api_error      — POST multipart -> API 400 -> error shown
8.  test_imports_bank_apply_success          — POST /admin/imports/bank/apply -> API 200 -> done page
9.  test_imports_bank_apply_api_error        — POST apply -> API 422 -> 303 /admin/imports/bank
10. test_imports_coa_requires_auth           — GET /admin/imports/coa without session -> 303
11. test_imports_coa_renders                 — GET /admin/imports/coa shows upload form + export link
12. test_imports_coa_preview_requires_auth   — POST /admin/imports/coa/preview without session -> 303
13. test_imports_coa_preview_success         — POST multipart -> API 200 -> diff preview rendered
14. test_imports_coa_apply_success           — POST apply -> API redirect -> 303 with flash
15. test_imports_nav_link                    — GET /accounts shows Imports nav link
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

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_API_BASE = settings.api_url.rstrip("/")
_ACCT_ID = "aaaaaaaa-0000-0000-0000-000000000002"

_MOCK_BANK_ACCT = {
    "id": _ACCT_ID,
    "code": "1010",
    "name": "Business Cheque",
}

_BANK_PREVIEW_HTML = """
<div class="bank-preview">
  <table>
    <thead><tr><th>Date</th><th>Description</th><th>Amount</th></tr></thead>
    <tbody>
      <tr><td>2026-04-01</td><td>OFFICE SUPPLIES</td><td>-250.00</td></tr>
    </tbody>
  </table>
  <form method="post" action="/admin/imports/bank/apply">
    <input type="hidden" name="account_id" value="{{ account_id }}">
    <input type="hidden" name="raw" value="date,description,amount\n2026-04-01,OFFICE SUPPLIES,-250.00">
    <button type="submit">Confirm Import</button>
  </form>
</div>
"""

_BANK_DONE_HTML = """
<div class="bank-done">
  <p>Imported 1 of 1 lines. 0 duplicates skipped.</p>
</div>
"""

_COA_PREVIEW_HTML = """
<div class="coa-diff">
  <h2>Changes Preview</h2>
  <table>
    <thead><tr><th>Code</th><th>Name</th><th>Change</th></tr></thead>
    <tbody>
      <tr><td>1010</td><td>Cash</td><td>NEW</td></tr>
    </tbody>
  </table>
  <form method="post" action="/admin/imports/coa/apply">
    <input type="hidden" name="raw" value="code,name,account_type\n1010,Cash,ASSET">
    <button type="submit">Apply Changes</button>
  </form>
</div>
"""


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-imports", "user_role": "admin"})


# ---------------------------------------------------------------------------
# 1. Landing auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_landing_requires_auth() -> None:
    """GET /admin/imports/ without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/imports/")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 2. Landing renders three options
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_landing_renders() -> None:
    """GET /admin/imports/ shows links to Bank, CoA, and QBO flows."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/imports/")

    assert resp.status_code == 200
    assert "Bank Statement" in resp.text
    assert "Chart of Accounts" in resp.text
    assert "QuickBooks" in resp.text
    assert "/admin/imports/bank" in resp.text
    assert "/admin/imports/coa" in resp.text


# ---------------------------------------------------------------------------
# 3. Bank form auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_bank_requires_auth() -> None:
    """GET /admin/imports/bank without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/imports/bank")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 4. Bank form renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_bank_renders(respx_mock: respx.MockRouter) -> None:
    """GET /admin/imports/bank shows file upload form with account picker."""
    respx_mock.get(f"{_API_BASE}/api/v1/bank-accounts").mock(
        return_value=Response(200, json={"items": [_MOCK_BANK_ACCT], "total": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/imports/bank")

    assert resp.status_code == 200
    assert "Business Cheque" in resp.text
    assert "Preview Import" in resp.text
    assert "enctype" in resp.text  # multipart form


# ---------------------------------------------------------------------------
# 5. Bank preview auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_bank_preview_requires_auth() -> None:
    """POST /admin/imports/bank/preview without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/imports/bank/preview", data={"account_id": _ACCT_ID})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 6. Bank preview success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_bank_preview_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/imports/bank/preview with API 200 -> preview HTML rendered.

    The Cat-C rewrite proxies through the wizard API (POST /api/v1/imports/wizards
    to start, then POST .../{id}/step to record the uploaded content) rather than
    a single passthrough call to /admin/imports/bank/preview.
    """
    respx_mock.post(f"{_API_BASE}/api/v1/imports/wizards").mock(
        return_value=Response(201, json={"wizard_id": "wiz-bank-1"})
    )
    respx_mock.post(
        url__regex=rf"^{_API_BASE}/api/v1/imports/wizards/[^/]+/step$"
    ).mock(return_value=Response(200, json={"status": "ok"}))

    csv_content = b"date,description,amount\n2026-04-01,OFFICE SUPPLIES,-250.00\n"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/imports/bank/preview",
            files={"file": ("statement.csv", csv_content, "text/csv")},
            data={"account_id": _ACCT_ID},
        )

    assert resp.status_code == 200
    assert "OFFICE SUPPLIES" in resp.text or "bank-preview" in resp.text


# ---------------------------------------------------------------------------
# 7. Bank preview API error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_bank_preview_api_error(respx_mock: respx.MockRouter) -> None:
    """POST /admin/imports/bank/preview with API 400 -> error shown on page.

    With no in-session wizard, the route starts one first via
    POST /api/v1/imports/wizards; a non-2xx there surfaces as the error.
    """
    respx_mock.post(f"{_API_BASE}/api/v1/imports/wizards").mock(
        return_value=Response(400, json={"detail": "Unrecognised CSV format"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/imports/bank/preview",
            data={"account_id": _ACCT_ID},
        )

    assert resp.status_code == 400
    assert "Unrecognised" in resp.text or "error" in resp.text.lower()


# ---------------------------------------------------------------------------
# 8. Bank apply success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_bank_apply_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/imports/bank/apply with API 200 -> done page rendered.

    Legacy direct-form path (raw + account_id, no wizard_id in session):
    the route starts a wizard, steps it with the raw content, then commits.
    """
    respx_mock.post(f"{_API_BASE}/api/v1/imports/wizards").mock(
        return_value=Response(201, json={"wizard_id": "wiz-bank-2"})
    )
    respx_mock.post(
        url__regex=rf"^{_API_BASE}/api/v1/imports/wizards/[^/]+/step$"
    ).mock(return_value=Response(200, json={"status": "ok"}))
    respx_mock.post(
        url__regex=rf"^{_API_BASE}/api/v1/imports/wizards/[^/]+/commit$"
    ).mock(return_value=Response(200, json={"inserted": 1, "total": 1}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/imports/bank/apply",
            data={
                "account_id": _ACCT_ID,
                "raw": "date,description,amount\n2026-04-01,OFFICE SUPPLIES,-250.00",
            },
        )

    assert resp.status_code == 200
    assert "Import" in resp.text or "bank-done" in resp.text


# ---------------------------------------------------------------------------
# 9. Bank apply API error -> redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_bank_apply_api_error(respx_mock: respx.MockRouter) -> None:
    """POST /admin/imports/bank/apply with API 422 -> 303 back to /admin/imports/bank."""
    respx_mock.post(f"{_API_BASE}/admin/imports/bank/apply").mock(
        return_value=Response(422, json={"detail": "Unknown bank account"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/imports/bank/apply",
            data={"account_id": _ACCT_ID, "raw": ""},
        )

    assert resp.status_code == 303
    assert "/admin/imports/bank" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 10. CoA form auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_coa_requires_auth() -> None:
    """GET /admin/imports/coa without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/admin/imports/coa")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 11. CoA form renders with export link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_coa_renders() -> None:
    """GET /admin/imports/coa shows upload form and export link."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/admin/imports/coa")

    assert resp.status_code == 200
    assert "Chart of Accounts" in resp.text
    assert "Preview Changes" in resp.text
    assert "/admin/imports/coa/export" in resp.text


# ---------------------------------------------------------------------------
# 12. CoA preview auth gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_imports_coa_preview_requires_auth() -> None:
    """POST /admin/imports/coa/preview without session -> 303 /login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.post("/admin/imports/coa/preview", data={})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 13. CoA preview success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_coa_preview_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/imports/coa/preview with API 200 -> diff preview rendered.

    The Cat-C rewrite proxies through the wizard API (POST /api/v1/imports/wizards
    to start, then POST .../{id}/step to record the uploaded CSV) and renders a
    lightweight row-count preview server-side rather than a diff table.
    """
    respx_mock.post(f"{_API_BASE}/api/v1/imports/wizards").mock(
        return_value=Response(201, json={"wizard_id": "wiz-coa-1"})
    )
    respx_mock.post(
        url__regex=rf"^{_API_BASE}/api/v1/imports/wizards/[^/]+/step$"
    ).mock(return_value=Response(200, json={"status": "ok"}))

    coa_csv = b"code,name,account_type,parent_code,tax_code_default,reconcile\n1010,Cash,ASSET,,,false\n"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.post(
            "/admin/imports/coa/preview",
            files={"file": ("coa.csv", coa_csv, "text/csv")},
        )

    assert resp.status_code == 200
    assert "Preview" in resp.text
    assert "account rows to import" in resp.text


# ---------------------------------------------------------------------------
# 14. CoA apply success — follows redirect
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_coa_apply_success(respx_mock: respx.MockRouter) -> None:
    """POST /admin/imports/coa/apply with API 303 -> redirect to CoA page.

    Legacy direct-form path (raw only, no wizard_id in session): the route
    starts a wizard, steps it with the raw content, then commits via
    POST /api/v1/imports/wizards/{id}/commit and redirects with the commit
    result as query params.
    """
    respx_mock.post(f"{_API_BASE}/api/v1/imports/wizards").mock(
        return_value=Response(201, json={"wizard_id": "wiz-coa-2"})
    )
    respx_mock.post(
        url__regex=rf"^{_API_BASE}/api/v1/imports/wizards/[^/]+/step$"
    ).mock(return_value=Response(200, json={"status": "ok"}))
    respx_mock.post(
        url__regex=rf"^{_API_BASE}/api/v1/imports/wizards/[^/]+/commit$"
    ).mock(return_value=Response(200, json={"added": 1, "updated": 0}))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/admin/imports/coa/apply",
            data={
                "raw": "code,name,account_type\n1010,Cash,ASSET",
            },
        )

    assert resp.status_code == 303
    assert "/admin/imports/coa" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 15. Imports nav link
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_imports_nav_link(respx_mock: respx.MockRouter) -> None:
    """GET /accounts shows Imports nav link in setup sub-row."""
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/accounts")

    assert resp.status_code == 200
    assert "/admin/imports" in resp.text
    assert "Imports" in resp.text
