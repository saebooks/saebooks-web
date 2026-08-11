"""The manifest and service worker must follow the active brand.

These two were the last hardcoded "SAE Books" strings outside a template, and
they are the two that persist worst when wrong: an installed PWA caches the
manifest name and icons at install time, so a stale manifest leaves the old
identity on the user's home screen until they reinstall, and the SW's offline
page is the one screen shown when nothing else is reachable.
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks_web.main import app


async def _get(path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.anyio
async def test_manifest_defaults_to_tasur() -> None:
    resp = await _get("/manifest.webmanifest")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    doc = json.loads(resp.text)
    assert doc["name"] == "Tasur"
    assert doc["short_name"] == "Tasur"
    assert "SAE Books" not in resp.text
    # Neutral surface: no jurisdiction in the copy, no AU-only acronyms.
    assert "Australian" not in doc["description"]
    assert "BAS" not in doc["description"]
    assert doc["lang"] == "en"


@pytest.mark.anyio
async def test_manifest_icons_follow_the_brand() -> None:
    doc = json.loads((await _get("/manifest.webmanifest")).text)

    by_purpose = {i["purpose"]: i for i in doc["icons"]}
    assert by_purpose["any"]["src"].startswith("/static/brand/tasur-icon-")
    assert by_purpose["maskable"]["src"] == "/static/brand/tasur-icon-maskable-512.png"
    # All three renditions must be present or install prompts degrade.
    assert {i["sizes"] for i in doc["icons"]} == {"192x192", "512x512"}
    assert len(doc["icons"]) == 3


@pytest.mark.anyio
async def test_manifest_saebooks_pin_restores_au_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    doc = json.loads((await _get("/manifest.webmanifest")).text)

    assert doc["name"] == "SAE Books"
    assert doc["lang"] == "en-AU"
    assert "BAS" in doc["description"]
    assert all(i["src"].startswith("/static/pwa/icons/") for i in doc["icons"])


@pytest.mark.anyio
async def test_manifest_tasur_ee_is_estonian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "tasur-ee")
    doc = json.loads((await _get("/manifest.webmanifest")).text)

    assert doc["name"] == "Tasur"
    assert doc["lang"] == "et"
    assert "KMD" in doc["description"]
    # Same Selge icons as the neutral brand — market splits copy, not identity.
    assert all(i["src"].startswith("/static/brand/") for i in doc["icons"])


@pytest.mark.anyio
async def test_manifest_keeps_structural_fields() -> None:
    """Only brand-derived fields are overridden; the rest survives untouched."""
    doc = json.loads((await _get("/manifest.webmanifest")).text)

    assert doc["display"] == "standalone"
    assert doc["scope"] == "/"
    assert doc["start_url"] == "/"
    assert doc["categories"] == ["business", "finance", "productivity"]
    assert [s["url"] for s in doc["shortcuts"]] == [
        "/inbox",
        "/invoices/new",
        "/",
        "/reports",
    ]


@pytest.mark.anyio
async def test_manifest_json_alias_matches() -> None:
    assert json.loads((await _get("/manifest.json")).text) == json.loads(
        (await _get("/manifest.webmanifest")).text
    )


@pytest.mark.anyio
async def test_service_worker_offline_page_is_branded() -> None:
    resp = await _get("/sw.js")

    assert resp.status_code == 200
    assert resp.headers["service-worker-allowed"] == "/"
    assert resp.headers["cache-control"] == "no-cache"
    assert "<h1>Tasur</h1>" in resp.text
    assert "Tasur needs a connection to your ledger" in resp.text
    # The placeholder must be fully substituted — a leaked token would render
    # literally on the offline screen.
    assert "__BRAND_NAME__" not in resp.text
    assert "SAE Books" not in resp.text


@pytest.mark.anyio
async def test_service_worker_follows_brand_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAEBOOKS_BRAND", "saebooks")
    resp = await _get("/sw.js")

    assert "<h1>SAE Books</h1>" in resp.text
    assert "__BRAND_NAME__" not in resp.text
    assert "Tasur" not in resp.text
