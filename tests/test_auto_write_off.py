"""Tests for the auto bad-debt write-off CLI — Phase 2 / Task 10.

The job iterates companies with writeoff_mode==auto, finds POSTED invoices
past threshold with balance owing, and calls the engine write-off endpoint.
Engine HTTP is mocked with respx; we assert which invoices get written off
and that the run is idempotent (409 → skip, not fail).
"""
from __future__ import annotations

import importlib.util
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
import respx
from httpx import Response

# Load the standalone script as a module.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "auto_write_off.py"
_spec = importlib.util.spec_from_file_location("auto_write_off", _SCRIPT)
awo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(awo)  # type: ignore[union-attr]

_API_BASE = "http://test-engine:8042"
_COMPANY_AUTO = "11111111-1111-1111-1111-111111111111"
_COMPANY_REVIEW = "22222222-2222-2222-2222-222222222222"
_OLD_INV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_RECENT_INV = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_PAID_INV = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _inv(inv_id: str, due_days_ago: int, total: str, paid: str) -> dict:
    return {
        "id": inv_id,
        "number": f"INV-{inv_id[:4]}",
        "status": "POSTED",
        "total": total,
        "amount_paid": paid,
        "due_date": _iso(due_days_ago),
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_API_URL", _API_BASE)
    monkeypatch.setenv("SAEBOOKS_API_TOKEN", "TEST_TOKEN")


@pytest.mark.anyio
@respx.mock
async def test_auto_write_off_writes_only_eligible(respx_mock: respx.MockRouter) -> None:
    """Only the old, unpaid POSTED invoice in the auto company is written off."""
    companies = {
        "items": [
            {"id": _COMPANY_AUTO, "name": "Auto Co", "writeoff_mode": "auto",
             "writeoff_threshold_days": 90, "archived_at": None},
            {"id": _COMPANY_REVIEW, "name": "Review Co", "writeoff_mode": "review",
             "writeoff_threshold_days": 90, "archived_at": None},
        ],
        "total": 2,
    }
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json=companies)
    )
    invoices = {
        "items": [
            _inv(_OLD_INV, 200, "500.00", "0.00"),     # eligible
            _inv(_RECENT_INV, 30, "300.00", "0.00"),   # too recent
            _inv(_PAID_INV, 200, "400.00", "400.00"),  # paid off
        ],
        "total": 3,
    }
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json=invoices)
    )
    wo_route = respx_mock.post(
        f"{_API_BASE}/api/v1/invoices/{_OLD_INV}/write-off"
    ).mock(return_value=Response(200, json={"id": _OLD_INV, "status": "WRITTEN_OFF"}))
    # Guard rails: these must NOT be called.
    recent_route = respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_RECENT_INV}/write-off")
    paid_route = respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_PAID_INV}/write-off")

    rc = await awo.run()

    assert rc == 0
    assert wo_route.called, "eligible invoice was not written off"
    assert not recent_route.called, "recent invoice should not be written off"
    assert not paid_route.called, "paid invoice should not be written off"
    # The auto company's invoice list must have been queried with X-Company-Id.
    inv_call = next(
        c for c in respx_mock.calls if "/api/v1/invoices" in str(c.request.url)
    )
    assert inv_call.request.headers.get("X-Company-Id") == _COMPANY_AUTO


@pytest.mark.anyio
@respx.mock
async def test_auto_write_off_skips_non_auto_companies(respx_mock: respx.MockRouter) -> None:
    """A tenant with no auto-mode companies does nothing and exits 0."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={
            "items": [{"id": _COMPANY_REVIEW, "name": "Review Co",
                       "writeoff_mode": "review", "writeoff_threshold_days": 90,
                       "archived_at": None}],
            "total": 1,
        })
    )
    # No invoice route registered — if the job queried invoices, respx would 404.
    rc = await awo.run()
    assert rc == 0


@pytest.mark.anyio
@respx.mock
async def test_auto_write_off_idempotent_on_409(respx_mock: respx.MockRouter) -> None:
    """A 409 (already written off) is a skip, not a failure — rc stays 0."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={
            "items": [{"id": _COMPANY_AUTO, "name": "Auto Co", "writeoff_mode": "auto",
                       "writeoff_threshold_days": 90, "archived_at": None}],
            "total": 1,
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [_inv(_OLD_INV, 200, "500.00", "0.00")], "total": 1})
    )
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_OLD_INV}/write-off").mock(
        return_value=Response(409, json={"detail": "Invoice already written off"})
    )
    rc = await awo.run()
    assert rc == 0  # skip, not fail


@pytest.mark.anyio
@respx.mock
async def test_auto_write_off_reports_failure(respx_mock: respx.MockRouter) -> None:
    """A 422 from the engine surfaces as rc=2 (one or more write-offs failed)."""
    respx_mock.get(f"{_API_BASE}/api/v1/companies").mock(
        return_value=Response(200, json={
            "items": [{"id": _COMPANY_AUTO, "name": "Auto Co", "writeoff_mode": "auto",
                       "writeoff_threshold_days": 90, "archived_at": None}],
            "total": 1,
        })
    )
    respx_mock.get(f"{_API_BASE}/api/v1/invoices").mock(
        return_value=Response(200, json={"items": [_inv(_OLD_INV, 200, "500.00", "0.00")], "total": 1})
    )
    respx_mock.post(f"{_API_BASE}/api/v1/invoices/{_OLD_INV}/write-off").mock(
        return_value=Response(422, json={"detail": "Some validation error"})
    )
    rc = await awo.run()
    assert rc == 2


@pytest.mark.anyio
async def test_auto_write_off_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token → rc=1 config error."""
    monkeypatch.delenv("SAEBOOKS_API_TOKEN", raising=False)
    rc = await awo.run()
    assert rc == 1


def test_token_file_indirection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """@/path indirection reads the token from a file."""
    tf = tmp_path / "tok"
    tf.write_text("FILE_TOKEN_123\n")
    monkeypatch.setenv("SAEBOOKS_API_TOKEN", f"@{tf}")
    assert awo._resolve_token() == "FILE_TOKEN_123"
