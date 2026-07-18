"""Tests for the Document Inbox web routes — issue #33 phase 1.

Covers the /inbox list (tabs, upload zone, camera input, gates), the
review page (prefill, provenance badges, CSRF, preview pane), save
(PATCH extraction_override + 409 reload banner), publish (DRAFT expense,
X-Idempotency-Key, 422 degrade for BILL), reject, the file proxy, the
nav badge fragment and the supplier quick-create.
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

_DOC_ID = "aaaaaaaa-1111-2222-3333-444444444444"
_COMPANY_ID = "bbbbbbbb-1111-2222-3333-444444444444"
_CONTACT_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_PAYMENT_ACCOUNT_ID = "55555555-5555-5555-5555-555555555555"
_TAX_CODE_ID = "33333333-3333-3333-3333-333333333333"
_EXPENSE_ID = "99999999-9999-9999-9999-999999999999"


def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_SESSION_COOKIE = _make_session_cookie({"api_token": "test-token-inbox"})


def _doc(**overrides) -> dict:
    """One engine-shaped inbox document (api/v1/document_inbox.py shape)."""
    base = {
        "id": _DOC_ID,
        "company_id": None,
        "vault_file_id": "cccccccc-1111-2222-3333-444444444444",
        "sha256": "ab" * 32,
        "filename": "receipt.pdf",
        "mime": "application/pdf",
        "size_bytes": 12345,
        "source": "UPLOAD",
        "source_ref": None,
        "status": "NEEDS_REVIEW",
        "extract": {
            "vendor_name": "Acme Supplies Pty Ltd",
            "invoice_number": "INV-0042",
            "date": "2026-06-20",
            "due_date": None,
            "subtotal": "200.00",
            "tax_amount": "20.00",
            "total": "220.00",
            "currency": "AUD",
            "line_items": [
                {"description": "Widgets", "qty": 2, "unit_price": "100.00",
                 "amount": "200.00", "tax_code": "GST"},
            ],
            "notes": None,
        },
        "extraction_override": None,
        "extract_model": "claude-haiku-4-5",
        "extraction_confidence": "OK",
        "extraction_error": None,
        "extracted_at": "2026-07-04T01:00:00+00:00",
        "attempt_count": 1,
        "last_error": None,
        "duplicate_of_id": None,
        "published_record_kind": None,
        "published_record_id": None,
        "published_at": None,
        "reject_reason": None,
        "reject_note": None,
        "version": 1,
        "created_at": "2026-07-04T00:59:00+00:00",
        "updated_at": "2026-07-04T01:00:00+00:00",
    }
    base.update(overrides)
    return base


def _mock_dropdowns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(
            200,
            json={"items": [{"id": _CONTACT_ID, "name": "Acme Supplies Pty Ltd"}], "total": 1},
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/accounts").mock(
        return_value=Response(
            200,
            json={"items": [{"id": _ACCOUNT_ID, "name": "Office Expenses", "code": "6100"}], "total": 1},
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/tax_codes").mock(
        return_value=Response(
            200,
            json={"items": [{"id": _TAX_CODE_ID, "code": "GST", "rate": "10"}], "total": 1},
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(
            200,
            json={"items": [{"id": _COMPANY_ID, "name": "SAE Engineering"}], "total": 1},
        )
    )


def _client(**kwargs) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _SESSION_COOKIE},
        **kwargs,
    )


_STATS = {"RECEIVED": 1, "NEEDS_REVIEW": 2, "READY": 1, "FAILED": 0,
          "oldest_unextracted_age_s": 30}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inbox_list_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/inbox")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.anyio
@respx.mock
async def test_inbox_list_renders_rows_tabs_and_upload_zone(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents").mock(
        return_value=Response(200, json={"items": [_doc()], "total": 1, "limit": 50, "offset": 0})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/stats").mock(
        return_value=Response(200, json=_STATS)
    )

    async with _client() as client:
        resp = await client.get("/inbox")

    assert resp.status_code == 200
    # Row content: filename, vendor, date, total, UPPERCASE status chip, source, age.
    assert "receipt.pdf" in resp.text
    assert "Acme Supplies Pty Ltd" in resp.text
    assert "2026-06-20" in resp.text
    assert "$220.00" in resp.text
    assert "NEEDS_REVIEW" in resp.text
    assert "UPLOAD" in resp.text
    # Confidence chip.
    assert ">OK<" in resp.text
    # Status filter tabs.
    assert "/inbox?status=READY" in resp.text
    assert "/inbox?status=FAILED" in resp.text
    # Upload zone with mobile camera capture input.
    assert 'accept="image/*,application/pdf"' in resp.text
    assert 'capture="environment"' in resp.text
    # Upload form carries the CSRF token.
    assert 'name="csrf_token"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_list_flag_off_renders_disabled_page(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )
    async with _client() as client:
        resp = await client.get("/inbox")
    assert resp.status_code == 404
    assert "not enabled" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_list_vault_off_renders_disabled_page(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents").mock(
        return_value=Response(503, json={"detail": "vault not enabled"})
    )
    async with _client() as client:
        resp = await client.get("/inbox")
    assert resp.status_code == 503
    assert "saebooks-vault" in resp.text


# ---------------------------------------------------------------------------
# Nav badge fragment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_badge_counts_open_documents(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/stats").mock(
        return_value=Response(200, json=_STATS)
    )
    async with _client() as client:
        resp = await client.get("/inbox/_badge")
    assert resp.status_code == 200
    assert ">4<" in resp.text  # 1 + 2 + 1 + 0


@pytest.mark.anyio
@respx.mock
async def test_inbox_badge_empty_when_flag_off(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/stats").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )
    async with _client() as client:
        resp = await client.get("/inbox/_badge")
    assert resp.status_code == 200
    assert resp.text == ""


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_upload_redirects_to_review(respx_mock: respx.MockRouter) -> None:
    created = _doc()
    created["duplicate"] = False
    respx_mock.post(f"{_API_BASE}/api/v1/inbox/documents").mock(
        return_value=Response(201, json=created)
    )
    async with _client() as client:
        resp = await client.post(
            "/inbox/upload",
            files={"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/inbox/{_DOC_ID}"


@pytest.mark.anyio
@respx.mock
async def test_inbox_upload_duplicate_still_lands_on_review(
    respx_mock: respx.MockRouter,
) -> None:
    existing = _doc()
    existing["duplicate"] = True
    respx_mock.post(f"{_API_BASE}/api/v1/inbox/documents").mock(
        return_value=Response(200, json=existing)
    )
    async with _client() as client:
        resp = await client.post(
            "/inbox/upload",
            files={"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/inbox/{_DOC_ID}"


@pytest.mark.anyio
@respx.mock
async def test_inbox_upload_unsupported_type_flashes_back(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{_API_BASE}/api/v1/inbox/documents").mock(
        return_value=Response(
            422, json={"detail": "Unsupported file type 'text/plain'."}
        )
    )
    async with _client() as client:
        resp = await client.post(
            "/inbox/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inbox"


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_prefills_from_extract(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc())
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")

    assert resp.status_code == 200
    # Prefill from extract.
    assert 'value="Acme Supplies Pty Ltd"' in resp.text
    assert 'value="2026-06-20"' in resp.text
    assert 'value="INV-0042"' in resp.text
    assert 'value="220.00"' in resp.text
    assert 'value="Widgets"' in resp.text
    # Model-filled values are visually marked.
    assert ">AI<" in resp.text
    # PDF preview streams through the web proxy.
    assert f'<embed src="/inbox/{_DOC_ID}/file"' in resp.text
    # Record-kind selector renders BILL / CREDIT_NOTE options too.
    assert 'value="EXPENSE"' in resp.text
    assert 'value="BILL"' in resp.text
    assert 'value="CREDIT_NOTE"' in resp.text
    # Version + idempotency key travel in the form; CSRF token present.
    assert 'name="version" value="1"' in resp.text
    assert 'name="idempotency_key"' in resp.text
    assert 'name="csrf_token"' in resp.text
    # Variance bar hook + reject reasons.
    assert "inbox-variance" in resp.text
    assert "NOT_A_DOCUMENT" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_image_uses_img_tag(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(mime="image/jpeg", filename="photo.jpg"))
    )
    _mock_dropdowns(respx_mock)
    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")
    assert resp.status_code == 200
    assert f'<img src="/inbox/{_DOC_ID}/file"' in resp.text
    assert "<embed" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_override_marked_edited(respx_mock: respx.MockRouter) -> None:
    doc = _doc(
        extraction_override={"vendor_name": "Acme (corrected)", "contact_id": _CONTACT_ID},
        status="READY",
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=doc)
    )
    _mock_dropdowns(respx_mock)
    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")
    assert resp.status_code == 200
    # Override wins the prefill and is marked EDITED.
    assert 'value="Acme (corrected)"' in resp.text
    assert ">EDITED<" in resp.text
    assert "READY" in resp.text


# ---------------------------------------------------------------------------
# Save (PATCH extraction_override)
# ---------------------------------------------------------------------------


def _review_form(**overrides) -> dict[str, str]:
    form = {
        "action": "save",
        "version": "1",
        "idempotency_key": "11112222-3333-4444-5555-666677778888",
        "record_kind": "EXPENSE",
        "company_id": _COMPANY_ID,
        "vendor_name": "Acme Supplies Pty Ltd",
        "contact_id": _CONTACT_ID,
        "date": "2026-06-20",
        "invoice_number": "INV-0042",
        "payment_account_id": _PAYMENT_ACCOUNT_ID,
        "total": "220.00",
        "notes": "",
        "lines[0][description]": "Widgets",
        "lines[0][account_id]": _ACCOUNT_ID,
        "lines[0][tax_code_id]": _TAX_CODE_ID,
        "lines[0][quantity]": "2",
        "lines[0][unit_price]": "100.00",
    }
    form.update(overrides)
    return form


@pytest.mark.anyio
@respx.mock
async def test_inbox_save_patches_override(respx_mock: respx.MockRouter) -> None:
    patch_route = respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2, status="READY"))
    )
    async with _client() as client:
        resp = await client.post(f"/inbox/{_DOC_ID}/review", data=_review_form())

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/inbox/{_DOC_ID}"
    assert patch_route.called
    body = _json.loads(patch_route.calls.last.request.content)
    assert body["version"] == 1
    override = body["extraction_override"]
    assert override["contact_id"] == _CONTACT_ID
    assert override["total"] == "220.00"
    assert override["line_items"][0]["account_id"] == _ACCOUNT_ID
    assert override["line_items"][0]["tax_code_id"] == _TAX_CODE_ID
    assert body["company_id"] == _COMPANY_ID


@pytest.mark.anyio
@respx.mock
async def test_inbox_save_version_conflict_shows_reload_banner(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(409, json={"detail": "version mismatch: expected 2, got 1"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2))
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.post(f"/inbox/{_DOC_ID}/review", data=_review_form())

    assert resp.status_code == 409
    assert "changed in another window" in resp.text
    # Re-rendered against the fresh version.
    assert 'name="version" value="2"' in resp.text


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_creates_draft_expense(respx_mock: respx.MockRouter) -> None:
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2, status="READY"))
    )
    publish_route = respx_mock.post(
        f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/publish"
    ).mock(
        return_value=Response(
            201,
            json={
                "document": _doc(version=3, status="PUBLISHED",
                                 published_record_kind="EXPENSE",
                                 published_record_id=_EXPENSE_ID),
                "record": {"kind": "EXPENSE", "id": _EXPENSE_ID, "status": "DRAFT"},
            },
        )
    )

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review", data=_review_form(action="publish")
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/expenses/{_EXPENSE_ID}"
    assert publish_route.called
    req = publish_route.calls.last.request
    # Idempotency key from the form travels on the header.
    assert req.headers["X-Idempotency-Key"] == "11112222-3333-4444-5555-666677778888"
    body = _json.loads(req.content)
    assert body["record_kind"] == "EXPENSE"
    assert body["company_id"] == _COMPANY_ID
    assert body["contact_id"] == _CONTACT_ID
    assert body["payment_account_id"] == _PAYMENT_ACCOUNT_ID
    assert body["date"] == "2026-06-20"
    assert body["reference"] == "INV-0042"
    assert body["lines"] == [
        {
            "description": "Widgets",
            "account_id": _ACCOUNT_ID,
            "tax_code_id": _TAX_CODE_ID,
            "quantity": "2",
            "unit_price": "100.00",
        }
    ]


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_bill_degrades_gracefully_on_422(
    respx_mock: respx.MockRouter,
) -> None:
    """Phase 1 engine 422s on BILL — the review page re-renders with the
    engine's message instead of blowing up."""
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2))
    )
    respx_mock.post(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/publish").mock(
        return_value=Response(
            422,
            json={"detail": "record_kind 'BILL' is not supported yet; "
                            "phase 1 publishes EXPENSE only"},
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2))
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review",
            data=_review_form(action="publish", record_kind="BILL"),
        )

    assert resp.status_code == 422
    assert "not supported yet" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_missing_contact_is_a_form_error(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2))
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review",
            data=_review_form(action="publish", contact_id=""),
        )

    assert resp.status_code == 422
    assert "Contact is required" in resp.text


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_reject_posts_reason_and_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    reject_route = respx_mock.post(
        f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/reject"
    ).mock(
        return_value=Response(200, json=_doc(status="REJECTED", reject_reason="PERSONAL"))
    )
    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/reject",
            data={"reason": "PERSONAL", "note": "coffee with family"},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inbox"
    body = _json.loads(reject_route.calls.last.request.content)
    assert body == {"reason": "PERSONAL", "note": "coffee with family"}


# ---------------------------------------------------------------------------
# File proxy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_file_streams_inline(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc())
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/download").mock(
        return_value=Response(
            200, content=b"%PDF-1.4 fake bytes",
            headers={"content-type": "application/pdf"},
        )
    )
    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.headers["content-disposition"].startswith("inline;")
    assert resp.content == b"%PDF-1.4 fake bytes"


# ---------------------------------------------------------------------------
# Supplier quick-create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_inbox_quick_contact_creates_and_selects(
    respx_mock: respx.MockRouter,
) -> None:
    new_id = "77777777-7777-7777-7777-777777777777"
    create_route = respx_mock.post(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(201, json={"id": new_id, "name": "Acme Supplies Pty Ltd"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(
            200,
            json={"items": [{"id": new_id, "name": "Acme Supplies Pty Ltd"}], "total": 1},
        )
    )
    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/quick-contact",
            data={"vendor_name": "Acme Supplies Pty Ltd"},
        )
    assert resp.status_code == 200
    body = _json.loads(create_route.calls.last.request.content)
    assert body == {"name": "Acme Supplies Pty Ltd", "contact_type": "SUPPLIER"}
    assert f'value="{new_id}" selected' in resp.text
    assert "Supplier created and selected." in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_quick_contact_empty_vendor_is_an_error(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/contacts").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/quick-contact", data={"vendor_name": "  "}
        )
    assert resp.status_code == 422
    assert "No vendor name" in resp.text


# ---------------------------------------------------------------------------
# Advisory near-duplicate banner (phase 4)
# ---------------------------------------------------------------------------

_SIBLING_ID = "dddddddd-1111-2222-3333-444444444444"


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_shows_advisory_duplicate_banner(
    respx_mock: respx.MockRouter,
) -> None:
    doc = _doc(
        advisory_duplicates=[
            {
                "id": _SIBLING_ID,
                "status": "READY",
                "source": "EMAIL",
                "filename": "invoice-rescan.pdf",
                "vendor_name": "Acme Supplies Pty Ltd",
                "invoice_number": "INV-0042",
                "total": "220.00",
                "created_at": "2026-07-03T22:00:00+00:00",
            }
        ]
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=doc)
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")

    assert resp.status_code == 200
    assert "inbox-advisory-duplicates" in resp.text
    assert "Possible duplicate" in resp.text
    assert f'href="/inbox/{_SIBLING_ID}"' in resp.text
    assert "invoice-rescan.pdf" in resp.text
    # Advisory only — the publish button is still rendered.
    assert 'value="publish"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_no_banner_without_advisory(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(advisory_duplicates=[]))
    )
    _mock_dropdowns(respx_mock)
    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")
    assert resp.status_code == 200
    assert "inbox-advisory-duplicates" not in resp.text


# ---------------------------------------------------------------------------
# Email-in address settings page (phase 4)
# ---------------------------------------------------------------------------

_ADDR_ID = "eeeeeeee-1111-2222-3333-444444444444"
_ADDR_ID_2 = "eeeeeeee-2222-3333-4444-555555555555"


def _email_address(**overrides) -> dict:
    base = {
        "id": _ADDR_ID,
        "token": "k7m2p9q4w8x3zr5t",
        "address": "k7m2p9q4w8x3zr5t@in.saebooks.com.au",
        "company_id": None,
        "active": True,
        "revoked_at": None,
        "created_at": "2026-07-04T01:00:00+00:00",
        "updated_at": "2026-07-04T01:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.mark.anyio
@respx.mock
async def test_email_addresses_page_lists_with_copy_button(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/email-addresses").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    _email_address(),
                    _email_address(
                        id=_ADDR_ID_2,
                        token="a1b2c3d4e5f6g7h8",
                        address="a1b2c3d4e5f6g7h8@in.saebooks.com.au",
                        active=False,
                        revoked_at="2026-07-03T00:00:00+00:00",
                    ),
                ]
            },
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(
            200, json={"items": [{"id": _COMPANY_ID, "name": "SAE Engineering"}]}
        )
    )

    async with _client() as client:
        resp = await client.get("/inbox/email-addresses")

    assert resp.status_code == 200
    assert "k7m2p9q4w8x3zr5t@in.saebooks.com.au" in resp.text
    assert "inboxCopyAddress" in resp.text  # copy button wiring
    assert ">ACTIVE<" in resp.text
    assert ">REVOKED<" in resp.text
    # Mint form + revoke form (active row only) + CSRF everywhere.
    assert 'action="/inbox/email-addresses/mint"' in resp.text
    assert f'action="/inbox/email-addresses/{_ADDR_ID}/revoke"' in resp.text
    assert f'action="/inbox/email-addresses/{_ADDR_ID_2}/revoke"' not in resp.text
    assert 'name="csrf_token"' in resp.text


@pytest.mark.anyio
@respx.mock
async def test_email_addresses_flag_off_hides_gracefully(
    respx_mock: respx.MockRouter,
) -> None:
    """FLAG_INBOX_EMAIL off (route 404) but the inbox itself alive
    (stats 200) → in-page explainer, no mint form, HTTP 200."""
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/email-addresses").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/stats").mock(
        return_value=Response(200, json=_STATS)
    )

    async with _client() as client:
        resp = await client.get("/inbox/email-addresses")

    assert resp.status_code == 200
    assert "Email-in is not available" in resp.text
    assert 'action="/inbox/email-addresses/mint"' not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_email_addresses_whole_inbox_off_renders_disabled_page(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/email-addresses").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/stats").mock(
        return_value=Response(404, json={"detail": "Not found"})
    )

    async with _client() as client:
        resp = await client.get("/inbox/email-addresses")

    assert resp.status_code == 404
    assert "Document Inbox is not enabled" in resp.text


@pytest.mark.anyio
@respx.mock
async def test_email_address_mint_posts_and_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    mint_route = respx_mock.post(f"{_API_BASE}/api/v1/inbox/email-addresses").mock(
        return_value=Response(201, json=_email_address(company_id=_COMPANY_ID))
    )
    async with _client() as client:
        resp = await client.post(
            "/inbox/email-addresses/mint", data={"company_id": _COMPANY_ID}
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inbox/email-addresses"
    body = _json.loads(mint_route.calls.last.request.content)
    assert body == {"company_id": _COMPANY_ID}


@pytest.mark.anyio
@respx.mock
async def test_email_address_revoke_posts_and_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    revoke_route = respx_mock.post(
        f"{_API_BASE}/api/v1/inbox/email-addresses/{_ADDR_ID}/revoke"
    ).mock(
        return_value=Response(
            200, json=_email_address(active=False, revoked_at="2026-07-04T02:00:00+00:00")
        )
    )
    async with _client() as client:
        resp = await client.post(f"/inbox/email-addresses/{_ADDR_ID}/revoke")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inbox/email-addresses"
    assert revoke_route.called


# ---------------------------------------------------------------------------
# Progressive-web-app manifest (phase 4) — /inbox pins to a home screen
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_manifest_has_inbox_shortcut() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    manifest = _json.loads(resp.content)
    shortcut_urls = [s["url"] for s in manifest.get("shortcuts", [])]
    assert "/inbox" in shortcut_urls
    # Standalone display + icons — the installability baseline.
    assert manifest["display"] == "standalone"
    assert manifest["icons"]


# ---------------------------------------------------------------------------
# Adversarial-review fix pass — RECEIVED gating, BILL payment account,
# publish-409 messaging, rule-suggestion prefill, line-index counter
# ---------------------------------------------------------------------------

_BILL_ID = "77777777-7777-7777-7777-777777777777"
_CREDIT_NOTE_ID = "88888888-8888-8888-8888-888888888888"


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_received_hides_publish_button(
    respx_mock: respx.MockRouter,
) -> None:
    """RECEIVED → PUBLISHED is illegal engine-side — the form must not
    offer a publish that can only 409. Save/reject stay available."""
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(status="RECEIVED"))
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")

    assert resp.status_code == 200
    assert 'value="publish"' not in resp.text
    assert 'value="save"' in resp.text  # review edits still saveable
    assert "re-run extraction" in resp.text.lower()


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_received_blocked_before_engine(
    respx_mock: respx.MockRouter,
) -> None:
    """A forced publish POST on a RECEIVED document is answered with the
    real diagnosis — no engine publish call (respx would 500 on an
    unmocked route), no misleading 'changed in another window' banner."""
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2, status="RECEIVED"))
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review", data=_review_form(action="publish")
        )

    assert resp.status_code == 409
    assert "hasn&#39;t been extracted yet" in resp.text or "hasn't been extracted yet" in resp.text
    assert "changed in another window" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_bill_sends_no_payment_account(
    respx_mock: respx.MockRouter,
) -> None:
    """The engine wants payment_account_id for EXPENSE only — a BILL
    publish must pass web validation without one and must not smuggle a
    meaningless 'Paid from' into the payload."""
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2, status="READY"))
    )
    publish_route = respx_mock.post(
        f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/publish"
    ).mock(
        return_value=Response(
            201,
            json={
                "document": _doc(version=3, status="PUBLISHED",
                                 published_record_kind="BILL",
                                 published_record_id=_BILL_ID),
                "record": {"kind": "BILL", "id": _BILL_ID, "status": "DRAFT"},
            },
        )
    )

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review",
            data=_review_form(
                action="publish", record_kind="BILL", payment_account_id=""
            ),
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/bills/{_BILL_ID}"
    body = _json.loads(publish_route.calls.last.request.content)
    assert body["record_kind"] == "BILL"
    assert "payment_account_id" not in body


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_credit_note_sends_no_payment_account(
    respx_mock: respx.MockRouter,
) -> None:
    """Same as the BILL case — a CREDIT_NOTE publish must pass web
    validation without a payment account and must not smuggle a
    meaningless 'Paid from' into the payload."""
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2, status="READY"))
    )
    publish_route = respx_mock.post(
        f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/publish"
    ).mock(
        return_value=Response(
            201,
            json={
                "document": _doc(version=3, status="PUBLISHED",
                                 published_record_kind="CREDIT_NOTE",
                                 published_record_id=_CREDIT_NOTE_ID),
                "record": {"kind": "CREDIT_NOTE", "id": _CREDIT_NOTE_ID},
            },
        )
    )

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review",
            data=_review_form(
                action="publish", record_kind="CREDIT_NOTE", payment_account_id=""
            ),
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/credit-notes/{_CREDIT_NOTE_ID}"
    body = _json.loads(publish_route.calls.last.request.content)
    assert body["record_kind"] == "CREDIT_NOTE"
    assert "payment_account_id" not in body


@pytest.mark.anyio
@respx.mock
async def test_inbox_publish_409_shows_engine_detail_not_version_banner(
    respx_mock: respx.MockRouter,
) -> None:
    """A publish 409 is an illegal state transition, not an optimistic-
    lock miss — surface the engine's message, not the reload banner."""
    respx_mock.patch(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc(version=2, status="READY"))
    )
    respx_mock.post(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}/publish").mock(
        return_value=Response(
            409,
            json={"detail": "illegal transition PUBLISHED -> PUBLISHED"},
        )
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(
            200,
            json=_doc(version=3, status="PUBLISHED",
                      published_record_kind="EXPENSE",
                      published_record_id=_EXPENSE_ID),
        )
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.post(
            f"/inbox/{_DOC_ID}/review", data=_review_form(action="publish")
        )

    assert resp.status_code == 409
    assert "illegal transition" in resp.text
    assert "changed in another window" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_prefills_rule_suggestions(
    respx_mock: respx.MockRouter,
) -> None:
    """A document promoted to READY purely by a supplier rule renders
    with the suggested contact/account/tax preselected, so READY really
    is one-click-publishable."""
    doc = _doc(
        status="READY",
        extract={
            "vendor_name": "Acme Supplies Pty Ltd",
            "invoice_number": "INV-0042",
            "date": "2026-06-20",
            "total": "220.00",
            "currency": "AUD",
            "line_items": [
                {"description": "Widgets", "qty": 2, "unit_price": "100.00",
                 "amount": "200.00"},
            ],
        },
        suggested_contact_id=_CONTACT_ID,
        suggested_account_id=_ACCOUNT_ID,
        suggested_tax_code_id=_TAX_CODE_ID,
    )
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=doc)
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")

    assert resp.status_code == 200
    assert f'value="{_CONTACT_ID}" selected' in resp.text
    assert f'value="{_ACCOUNT_ID}" selected' in resp.text
    assert f'value="{_TAX_CODE_ID}" selected' in resp.text
    # No stray quick-create confirmation on a plain review render.
    assert "Supplier created and selected" not in resp.text


@pytest.mark.anyio
@respx.mock
async def test_inbox_review_line_index_is_monotonic_counter(
    respx_mock: respx.MockRouter,
) -> None:
    """Client-side add-line must never reuse the row COUNT as the index
    (remove middle row + add → collision silently drops a coded line);
    the page seeds a monotonic counter from the rendered line count."""
    respx_mock.get(f"{_API_BASE}/api/v1/inbox/documents/{_DOC_ID}").mock(
        return_value=Response(200, json=_doc())
    )
    _mock_dropdowns(respx_mock)

    async with _client() as client:
        resp = await client.get(f"/inbox/{_DOC_ID}")

    assert resp.status_code == 200
    assert "var inboxLineIdx = 1;" in resp.text  # one extracted line
    assert "inboxLineIdx++" in resp.text
    assert ".inbox-line-row').length" not in resp.text
