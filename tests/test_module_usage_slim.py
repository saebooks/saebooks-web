"""fetch_module_usage must store a cookie-sized snapshot.

The session rides in a signed cookie with a hard 4096-byte browser limit.
The raw /api/v1/modules/usage payload (30+ modules, caps blocks) blew past
it and real email/password logins silently looped: the 303 carried a
Set-Cookie the browser refused to store. These tests pin the slimming
transform and the list→dict re-keying merged_module_registry depends on.
"""
from __future__ import annotations

import json

from saebooks_web.module_registry import _slim_usage, merged_module_registry

_RAW = {
    "edition": "enterprise",
    "effective_edition": "enterprise",
    "bookkeeping_mode": "cashbook",
    "caps": {
        "admin_seats": {"outcome": "allow", "limit": None, "current": 1, "reason": ""},
        "employee_seats": {"outcome": "allow", "limit": None, "current": 0, "reason": ""},
        "companies": {"outcome": "allow", "limit": None, "current": 1, "reason": ""},
    },
    "modules": [
        {"id": "bank_feeds", "kind": "flag", "entitled": True, "health": "ok"},
        {"id": "asset_forecasts", "kind": "delegated", "entitled": False, "health": "down"},
        {"kind": "flag", "entitled": True},  # no id — dropped, not crashed
    ],
}


def test_slim_keeps_only_consumed_fields() -> None:
    slim = _slim_usage(_RAW)
    assert set(slim) == {"edition", "effective_edition", "modules"}
    assert slim["effective_edition"] == "enterprise"
    assert slim["modules"] == {
        "bank_feeds": {"entitled": True, "health": "ok"},
        "asset_forecasts": {"entitled": False, "health": "down"},
    }


def test_slim_rekeys_list_for_nav_merge() -> None:
    # merged_module_registry looks modules up by id — the raw list shape
    # made it raise (and the middleware swallow), leaving nav undecorated.
    catalogue = {
        "modules": [
            {"id": "bank_feeds", "label": "Bank feeds", "kind": "flag",
             "group": "extras", "state": "ga"},
        ]
    }
    merged = merged_module_registry(catalogue, _slim_usage(_RAW))
    assert merged["bank_feeds"]["entitled"] is True
    assert merged["bank_feeds"]["health"] == "ok"


def test_slim_tolerates_dict_shaped_modules() -> None:
    slim = _slim_usage(
        {"effective_edition": "pro",
         "modules": {"bank_feeds": {"entitled": True, "health": "ok"}}}
    )
    assert slim["modules"] == {"bank_feeds": {"entitled": True, "health": "ok"}}


def test_slim_snapshot_fits_in_a_cookie_at_scale() -> None:
    # 40 modules — comfortably above today's 31 — must leave generous room
    # for the JWT and the rest of the session inside the 4096-byte cap.
    raw = {
        "edition": "enterprise",
        "effective_edition": "enterprise",
        "caps": _RAW["caps"],
        "modules": [
            {"id": f"module_{n:02d}", "kind": "flag", "entitled": True, "health": "ok"}
            for n in range(40)
        ],
    }
    assert len(json.dumps(_slim_usage(raw))) < 2500
