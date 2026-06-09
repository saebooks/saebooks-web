"""Tests for the CONTRACTOR / SUB_CONTRACTOR contact types in the web UI.

Engine enum 0163 added CONTRACTOR and SUB_CONTRACTOR to contact_type_enum.
These tests cover the saebooks-web (consumer) side:

1.  Create with contact_type=CONTRACTOR round-trips to the API (303 + body).
2.  Create with contact_type=SUB_CONTRACTOR round-trips to the API.
3.  The contacts list filter forwards ?contact_type=CONTRACTOR as ?type=CONTRACTOR.
4.  The contacts list filter forwards ?contact_type=SUB_CONTRACTOR as ?type=SUB_CONTRACTOR.
5.  GET /contacts/new renders the new options + the TPAR checkbox + the
    SUB_CONTRACTOR default JS.
6.  Create with is_tpar_supplier=on includes is_tpar_supplier: True in the payload.
7.  Create without the TPAR checkbox sends is_tpar_supplier: False.
8.  The list view renders distinct badges (never the raw \"SUB_CONTRACTOR\").
9.  The detail view renders distinct badges for the new types.
10. The Bill new-form payee dropdown queries CONTRACTOR + SUB_CONTRACTOR contacts.

NOTE on TPAR persistence: as of engine alembic 0163 the v1 JSON API schemas
(ContactCreate / ContactUpdate / ContactOut) do NOT expose is_tpar_supplier,
so the field is silently dropped by the engine on create/update and is not
returned on read. The web UI sends it anyway (forward-compatible), and these
tests assert only that the *web payload* carries the flag — NOT that the engine
persists it. Persistence is blocked on an engine-lane spec (see PR body).
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

_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

_MOCK_ACCOUNT = {"id": _ACCOUNT_ID, "name": "Subcontractors", "code": "5000", "account_type": "EXPENSE"}
_MOCK_ACCOUNTS = {"items": [_MOCK_ACCOUNT], "total": 1, "limit": 1000, "offset": 0}

_TAX_CODE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_MOCK_TAX_CODE = {"id": _TAX_CODE_ID, "code": "GST", "name": "GST on Expenses", "rate": "10.000"}
_MOCK_TAX_CODES = {"items": [_MOCK_TAX_CODE], "total": 1, "page": 1, "page_size": 500}

_EMPTY_LIST = {"items": [], "total": 0, "limit": 500, "offset": 0}


def _contact(contact_type: str, name: str = "Acme Trades") -> dict:
    return {
        "id": _CONTACT_ID,
        "name": name,
        "contact_type": contact_type,
        "email": None,
        "phone": None,
        "abn": None,
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "state": None,
        "postcode": None,
        "country": "Australia",
        "notes": None,
        "default_account_id": None,
        "default_tax_code": None,
        "bank_bsb": None,
        "bank_account_number": None,
        "bank_account_title": None,
        "currency_code": None,
        "is_one_off": False,
        "company_id": "22222222-2222-2222-2222-222222222222",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "version": 1,
        "archived_at": None,
        "created_at": "2026-06-07T00:00:00Z",
        "updated_at": "2026-06-07T00:00:00Z",
    }


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-abc"})
_API_BASE = settings.api_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1 + 2. Create with the new types round-trips to the API.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctype", ["CONTRACTOR", "SUB_CONTRACTOR"])
@pytest.mark.anyio
@respx.mock
async def test_contact_create_new_type_round_trips(respx_mock: respx.MockRouter, ctype: str) -> None:
    captured: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json=_contact(ctype))

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        resp = await client.post(
            "/contacts/new",
            data={
                "name": "Acme Trades",
                "contact_type": ctype,
                "idempotency_key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/contacts/{_CONTACT_ID}"
    assert len(captured) == 1
    assert captured[0]["contact_type"] == ctype


# ---------------------------------------------------------------------------
# 3 + 4. List filter forwards the new types as ?type=.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctype", ["CONTRACTOR", "SUB_CONTRACTOR"])
@pytest.mark.anyio
@respx.mock
async def test_contacts_list_filter_accepts_new_types(respx_mock: respx.MockRouter, ctype: str) -> None:
    captured_params: list[str] = []

    def _capture(request: respx.Request) -> Response:
        captured_params.append(request.url.params.get("type", ""))
        return Response(200, json={"items": [_contact(ctype)], "total": 1})

    respx_mock.get(f"{_API_BASE}/api/v1/contacts/one-off-candidates").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts?contact_type={ctype}")

    assert resp.status_code == 200
    # The new type must NOT be silently dropped by the whitelist — it must be
    # forwarded to the API as ?type=<ctype>.
    assert ctype in captured_params, f"expected ?type={ctype} forwarded, got {captured_params}"


# ---------------------------------------------------------------------------
# 5. New form renders the new options + the TPAR checkbox + default JS.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_new_form_has_new_types_and_tpar(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(return_value=Response(200, json=_MOCK_ACCOUNTS))
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(return_value=Response(200, json=_MOCK_TAX_CODES))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/contacts/new")

    assert resp.status_code == 200
    body = resp.text
    assert 'value="CONTRACTOR"' in body
    assert 'value="SUB_CONTRACTOR"' in body
    assert "Sub-contractor" in body  # human-friendly label, not raw enum
    # TPAR checkbox is present.
    assert 'name="is_tpar_supplier"' in body
    # The default JS keys off SUB_CONTRACTOR.
    assert 'typeSel.value === "SUB_CONTRACTOR"' in body


# ---------------------------------------------------------------------------
# 6. Create with the TPAR checkbox ticked sends is_tpar_supplier: True.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_tpar_checked_sent_in_payload(respx_mock: respx.MockRouter) -> None:
    captured: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json=_contact("SUB_CONTRACTOR"))

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/contacts/new",
            data={
                "name": "Subbie Co",
                "contact_type": "SUB_CONTRACTOR",
                "is_tpar_supplier": "on",
                "idempotency_key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        )

    assert len(captured) == 1
    assert captured[0].get("is_tpar_supplier") is True


# ---------------------------------------------------------------------------
# 7. Create WITHOUT the TPAR checkbox sends is_tpar_supplier: False.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contact_create_tpar_unchecked_sent_false(respx_mock: respx.MockRouter) -> None:
    captured: list[dict] = []

    def _capture(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content))
        return Response(201, json=_contact("CONTRACTOR"))

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        follow_redirects=False,
    ) as client:
        await client.post(
            "/contacts/new",
            data={
                "name": "Contractor Co",
                "contact_type": "CONTRACTOR",
                "idempotency_key": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        )

    assert len(captured) == 1
    assert captured[0].get("is_tpar_supplier") is False


# ---------------------------------------------------------------------------
# 8. List view renders distinct badges, never the raw enum.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_contacts_list_renders_distinct_badges(respx_mock: respx.MockRouter) -> None:
    rows = [_contact("CONTRACTOR", "Big Build Co"), _contact("SUB_CONTRACTOR", "Sparky Labour")]
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/one-off-candidates").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": rows, "total": 2})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/contacts")

    assert resp.status_code == 200
    assert ">Contractor<" in resp.text
    assert ">Sub-contractor<" in resp.text
    # The raw enum must never leak as a *displayed label* (between tags).
    # (It legitimately appears as the filter <option value="SUB_CONTRACTOR">.)
    assert ">SUB_CONTRACTOR<" not in resp.text


# ---------------------------------------------------------------------------
# 9. Detail view renders distinct badges for the new types.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctype,label", [("CONTRACTOR", "Contractor"), ("SUB_CONTRACTOR", "Sub-contractor")])
@pytest.mark.anyio
@respx.mock
async def test_contact_detail_renders_new_type_badge(respx_mock: respx.MockRouter, ctype: str, label: str) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=_contact(ctype, "Trade Co"))
    )
    # Detail fans out to txn endpoints + attachments; return empty for all.
    for path in ("invoices", "bills", "payments", "credit_notes", "expenses"):
        respx_mock.get(f"{_API_BASE}/api/v1/{path}").mock(return_value=Response(200, json=_EMPTY_LIST))
    respx_mock.get(f"{_API_BASE}/api/v1/attachments").mock(return_value=Response(200, json=[]))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}")

    assert resp.status_code == 200
    assert f">{label}<" in resp.text
    # Raw enum must not appear as a displayed label.
    assert ">SUB_CONTRACTOR<" not in resp.text


# ---------------------------------------------------------------------------
# 10. Bill new-form payee dropdown queries CONTRACTOR + SUB_CONTRACTOR.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_bill_new_form_payee_dropdown_includes_new_types(respx_mock: respx.MockRouter) -> None:
    queried_types: list[str] = []

    def _capture(request: respx.Request) -> Response:
        queried_types.append(request.url.params.get("type", ""))
        return Response(200, json=_EMPTY_LIST)

    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_capture)
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(return_value=Response(200, json=_MOCK_ACCOUNTS))
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(return_value=Response(200, json=_MOCK_TAX_CODES))
    respx_mock.get(f"{_API_BASE}/api/v1/projects").mock(return_value=Response(200, json=_EMPTY_LIST))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    ) as client:
        resp = await client.get("/bills/new")

    assert resp.status_code == 200
    assert "CONTRACTOR" in queried_types, queried_types
    assert "SUB_CONTRACTOR" in queried_types, queried_types
    # Existing payee types must still be queried.
    assert "SUPPLIER" in queried_types
    assert "BOTH" in queried_types
