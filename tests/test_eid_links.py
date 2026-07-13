"""Link-store tests — HMAC keying, masking, conflicts, unlink."""
from __future__ import annotations

import json

import pytest

from saebooks_web import eid_links

CODE = "40504040001"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "eid_links.json"
    monkeypatch.setenv("SAEBOOKS_EID_LINK_STORE", str(path))
    return path


def test_link_roundtrip_and_lookup() -> None:
    rec = eid_links.link("User@Example.com", CODE, "smart-id")
    assert rec["email"] == "user@example.com"
    assert rec["provider"] == "smart-id"

    found = eid_links.find_link(CODE)
    assert found is not None and found["email"] == "user@example.com"
    assert eid_links.find_link("60001019906") is None

    by_email = eid_links.find_link_for_email("USER@example.com")
    assert by_email is not None and by_email["masked"] == rec["masked"]


def test_personal_code_never_stored_in_plaintext(store) -> None:
    eid_links.link("user@example.com", CODE, "smart-id")
    raw = store.read_text()
    assert CODE not in raw
    data = json.loads(raw)
    (fp,) = data["links"].keys()
    # Key is the keyed fingerprint, 64 hex chars, not derivable without the
    # session secret.
    assert len(fp) == 64 and fp == eid_links.code_fingerprint(CODE)
    assert data["links"][fp]["masked"] == "405••••••01"


def test_mask_personal_code() -> None:
    assert eid_links.mask_personal_code(CODE) == "405••••••01"
    assert eid_links.mask_personal_code("123") == "•••"


def test_conflicting_link_raises() -> None:
    eid_links.link("first@example.com", CODE, "smart-id")
    with pytest.raises(ValueError):
        eid_links.link("second@example.com", CODE, "mobile-id")
    # Same owner re-linking is fine (idempotent replace).
    eid_links.link("first@example.com", CODE, "mobile-id")
    assert eid_links.find_link(CODE)["provider"] == "mobile-id"


def test_relink_replaces_previous_code_for_same_user() -> None:
    eid_links.link("user@example.com", CODE, "smart-id")
    eid_links.link("user@example.com", "60001019906", "smart-id")
    assert eid_links.find_link(CODE) is None
    assert eid_links.find_link("60001019906")["email"] == "user@example.com"


def test_unlink() -> None:
    eid_links.link("user@example.com", CODE, "smart-id")
    assert eid_links.unlink("user@example.com") is True
    assert eid_links.find_link(CODE) is None
    assert eid_links.unlink("user@example.com") is False


def test_missing_or_corrupt_store_fails_closed(store) -> None:
    assert eid_links.find_link(CODE) is None
    store.write_text("{not json")
    assert eid_links.find_link(CODE) is None
