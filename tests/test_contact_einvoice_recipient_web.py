"""Tests for the EE contact e-invoice-recipient surface.

Wires the engine contract: ``e_invoice_recipient`` (bool) +
``peppol_participant_id`` (str|null, max 64) round-trip on the contact
write endpoints, and are surfaced EE-gated on the form (checkbox + field)
and detail (badge + Peppol ID) pages.

Everything is stubbed at the HTTP boundary with respx, following
``test_contacts_edit.py``. Jurisdiction is resolved by
CompanyContextMiddleware off the companies + tax_codes calls, so those are
stubbed EE/AU via the shared ``_mock_companies`` / ``_mock_tax_codes``
helpers — the payload/UI must differ purely on that resolved jurisdiction.
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
from tests.test_jurisdiction_gating import (
    _AU_COMPANY,
    _EE_COMPANY,
    _mock_companies,
    _mock_tax_codes,
)

_CONTACT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ACCOUNT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_API_BASE = settings.api_url.rstrip("/")

_MOCK_ACCOUNTS = {
    "items": [{"id": _ACCOUNT_ID, "name": "Revenue", "code": "4000", "account_type": "INCOME"}],
    "total": 1,
    "limit": 1000,
    "offset": 0,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {
        "api_token": "test-token-einvoice-contact",
        # Pin the render locale to English so assertions match the source
        # strings. In production EE-jurisdiction pages render in the
        # visitor's locale (Estonian); the LocaleMiddleware honours this
        # session override above the jurisdiction default.
        "locale": "en",
    }
)

_MOCK_CONTACT_EE = {
    "id": _CONTACT_ID,
    "name": "Acme OU",
    "contact_type": "CUSTOMER",
    "email": "billing@acme.ee",
    "phone": None,
    "abn": None,
    "address_line1": None,
    "address_line2": None,
    "city": "Tallinn",
    "state": None,
    "postcode": "10111",
    "country": "Estonia",
    "notes": None,
    "default_account_id": None,
    "default_tax_code": None,
    "bank_bsb": None,
    "bank_account_number": None,
    "bank_account_title": None,
    "currency_code": "EUR",
    "company_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 5,
    "archived_at": None,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "e_invoice_recipient": True,
    "peppol_participant_id": "0191:10137025",
}
_MOCK_CONTACT_AU = {
    **_MOCK_CONTACT_EE,
    "name": "Acme Pty Ltd",
    "country": "Australia",
    "currency_code": None,
    "e_invoice_recipient": False,
    "peppol_participant_id": None,
}


def _client(follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=follow_redirects,
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


def _capture_patch(respx_mock: respx.MockRouter, captured: list[dict]) -> None:
    def _side(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content or b"{}"))
        return Response(200, json=_MOCK_CONTACT_EE)

    respx_mock.patch(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(side_effect=_side)


def _capture_post(respx_mock: respx.MockRouter, captured: list[dict]) -> None:
    def _side(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content or b"{}"))
        return Response(201, json={**_MOCK_CONTACT_EE, "id": _CONTACT_ID})

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_side)


# ---------------------------------------------------------------------------
# Write-through — the flag + peppol id round-trip only for EE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_edit_persists_recipient_flag_ee(respx_mock: respx.MockRouter) -> None:
    """EE: a ticked checkbox + peppol id reach the PATCH payload."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    captured: list[dict] = []
    _capture_patch(respx_mock, captured)

    async with _client() as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "Acme OU",
                "contact_type": "CUSTOMER",
                "version": "5",
                "e_invoice_recipient": "on",
                "peppol_participant_id": "0191:10137025",
            },
        )

    assert resp.status_code == 303
    assert captured and captured[0]["e_invoice_recipient"] is True
    assert captured[0]["peppol_participant_id"] == "0191:10137025"


@pytest.mark.asyncio
@respx.mock
async def test_edit_unticked_clears_and_blank_peppol_nulls_ee(
    respx_mock: respx.MockRouter,
) -> None:
    """EE: no checkbox → False; blank peppol → null (clears it)."""
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    captured: list[dict] = []
    _capture_patch(respx_mock, captured)

    async with _client() as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={"name": "Acme OU", "contact_type": "CUSTOMER", "version": "5"},
        )

    assert resp.status_code == 303
    assert captured[0]["e_invoice_recipient"] is False
    assert captured[0]["peppol_participant_id"] is None


@pytest.mark.asyncio
@respx.mock
async def test_edit_omits_recipient_fields_for_au(respx_mock: respx.MockRouter) -> None:
    """AU: the fields never enter the payload — byte-identical to before."""
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, "AU")
    captured: list[dict] = []
    _capture_patch(respx_mock, captured)

    async with _client() as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "Acme Pty Ltd",
                "contact_type": "CUSTOMER",
                "version": "5",
                # Even if a crafted request smuggles the field in, AU must drop it.
                "e_invoice_recipient": "on",
                "peppol_participant_id": "0191:10137025",
            },
        )

    assert resp.status_code == 303
    assert "e_invoice_recipient" not in captured[0]
    assert "peppol_participant_id" not in captured[0]


@pytest.mark.asyncio
@respx.mock
async def test_create_persists_recipient_flag_ee(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    captured: list[dict] = []
    _capture_post(respx_mock, captured)

    async with _client() as client:
        resp = await client.post(
            "/contacts/new",
            data={
                "name": "Acme OU",
                "contact_type": "CUSTOMER",
                "e_invoice_recipient": "on",
                "peppol_participant_id": "0191:10137025",
            },
        )

    assert resp.status_code == 303
    assert captured[0]["e_invoice_recipient"] is True
    assert captured[0]["peppol_participant_id"] == "0191:10137025"


# ---------------------------------------------------------------------------
# EE-gated UI — form control + detail badge
# ---------------------------------------------------------------------------


def _mock_edit_form(respx_mock: respx.MockRouter, contact: dict) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=contact)
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(200, json=_MOCK_ACCOUNTS)
    )


@pytest.mark.asyncio
@respx.mock
async def test_edit_form_shows_checkbox_for_ee(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    _mock_edit_form(respx_mock, _MOCK_CONTACT_EE)

    async with _client() as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/edit")

    assert resp.status_code == 200
    assert 'name="e_invoice_recipient"' in resp.text
    assert 'name="peppol_participant_id"' in resp.text
    # Pre-populated from the fetched contact.
    assert "0191:10137025" in resp.text
    assert "checked" in resp.text


@pytest.mark.asyncio
@respx.mock
async def test_edit_form_hides_checkbox_for_au(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, "AU")
    _mock_edit_form(respx_mock, _MOCK_CONTACT_AU)

    async with _client() as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}/edit")

    assert resp.status_code == 200
    assert 'name="e_invoice_recipient"' not in resp.text
    assert 'name="peppol_participant_id"' not in resp.text


def _mock_detail_fanout(respx_mock: respx.MockRouter, contact: dict) -> None:
    """Stub the contact detail page: the contact + its transaction fan-out
    (invoices/bills/payments/credit_notes/expenses) + attachments."""
    respx_mock.get(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(
        return_value=Response(200, json=contact)
    )
    _empty = {"items": [], "total": 0, "limit": 50, "offset": 0}
    for kind in ("invoices", "bills", "payments", "credit_notes", "expenses"):
        respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/{kind}(\?.*)?$").mock(
            return_value=Response(200, json=_empty)
        )
    respx_mock.get(url__regex=rf"^{_API_BASE}/api/v1/attachments(\?.*)?$").mock(
        return_value=Response(200, json=[])
    )


@pytest.mark.asyncio
@respx.mock
async def test_detail_shows_badge_for_ee(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    _mock_detail_fanout(respx_mock, _MOCK_CONTACT_EE)

    async with _client() as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}")

    assert resp.status_code == 200
    assert "E-invoice recipient" in resp.text
    assert "0191:10137025" in resp.text


@pytest.mark.asyncio
@respx.mock
async def test_detail_hides_einvoicing_card_for_au(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _AU_COMPANY)
    _mock_tax_codes(respx_mock, "AU")
    _mock_detail_fanout(respx_mock, _MOCK_CONTACT_AU)

    async with _client() as client:
        resp = await client.get(f"/contacts/{_CONTACT_ID}")

    assert resp.status_code == 200
    assert "E-invoice recipient" not in resp.text
