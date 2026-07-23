"""The web contact create/edit handlers must forward SEPA bank fields.

The handlers already forwarded the AU trio (bank_bsb / bank_account_number
/ bank_account_title) but omitted ``iban`` / ``bic`` from both the create
field list and ``_EDIT_FIELDS`` — so an EE contact's bank details entered on
the form never reached the engine. These pin that they now do.

Stubbed at the HTTP boundary with respx, following
``test_contact_einvoice_recipient_web.py``.
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
    _EE_COMPANY,
    _mock_companies,
    _mock_tax_codes,
)

_CONTACT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_API_BASE = settings.api_url.rstrip("/")

_IBAN = "EE471000001020145685"
_BIC = "EEUHEE2X"

_MOCK_CONTACT_EE = {
    "id": _CONTACT_ID,
    "name": "Acme OU",
    "contact_type": "SUPPLIER",
    "email": None,
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
    "iban": _IBAN,
    "bic": _BIC,
    "currency_code": "EUR",
    "company_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "version": 5,
    "archived_at": None,
    "created_at": "2026-04-23T00:00:00Z",
    "updated_at": "2026-04-23T00:00:00Z",
    "e_invoice_recipient": False,
    "peppol_participant_id": None,
}


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie(
    {"api_token": "test-token-bank-contact", "locale": "en"}
)


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
    )


def _capture_post(respx_mock: respx.MockRouter, captured: list[dict]) -> None:
    def _side(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content or b"{}"))
        return Response(201, json={**_MOCK_CONTACT_EE, "id": _CONTACT_ID})

    respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(side_effect=_side)


def _capture_patch(respx_mock: respx.MockRouter, captured: list[dict]) -> None:
    def _side(request: respx.Request) -> Response:
        captured.append(_json.loads(request.content or b"{}"))
        return Response(200, json=_MOCK_CONTACT_EE)

    respx_mock.patch(f"{_API_BASE}/api/v1/contacts/{_CONTACT_ID}").mock(side_effect=_side)


@pytest.mark.asyncio
@respx.mock
async def test_create_forwards_iban_bic(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    captured: list[dict] = []
    _capture_post(respx_mock, captured)

    async with _client() as client:
        resp = await client.post(
            "/contacts/new",
            data={
                "name": "Acme OU",
                "contact_type": "SUPPLIER",
                "iban": _IBAN,
                "bic": _BIC,
            },
        )

    assert resp.status_code == 303, resp.text
    assert captured, "create handler never called the engine"
    assert captured[0]["iban"] == _IBAN
    assert captured[0]["bic"] == _BIC


@pytest.mark.asyncio
@respx.mock
async def test_edit_forwards_iban_bic(respx_mock: respx.MockRouter) -> None:
    _mock_companies(respx_mock, _EE_COMPANY)
    _mock_tax_codes(respx_mock, "EE")
    captured: list[dict] = []
    _capture_patch(respx_mock, captured)

    async with _client() as client:
        resp = await client.post(
            f"/contacts/{_CONTACT_ID}/edit",
            data={
                "name": "Acme OU",
                "contact_type": "SUPPLIER",
                "version": "5",
                "iban": _IBAN,
                "bic": _BIC,
            },
        )

    assert resp.status_code == 303, resp.text
    assert captured, "edit handler never called the engine"
    assert captured[0]["iban"] == _IBAN
    assert captured[0]["bic"] == _BIC
