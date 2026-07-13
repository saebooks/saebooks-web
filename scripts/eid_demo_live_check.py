#!/usr/bin/env python3
"""Live verification of the eID providers against SK's DEMO environment.

Drives one real Smart-ID authentication (RP API v3, notification flow)
and one real Mobile-ID authentication (MID API v1) end-to-end through the
production code path in ``saebooks_web.eid_providers`` — start → poll →
certificate-chain + signature validation → identity extraction — using
SK's published demo relying-party credentials and auto-responding test
accounts. Free to run (demo env is not billed); makes NO production calls.

Usage:
    .venv/bin/python scripts/eid_demo_live_check.py

Demo accounts (docs: test_accounts.html, "Notification flows"):
    Smart-ID  PNOEE-40504040001  — automatic OK
    Mobile-ID +37200000766 / 60001019906 — automatic OK
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saebooks_web.eid_providers import (  # noqa: E402
    EidError,
    MobileIdProvider,
    SmartIdProvider,
)

SMARTID_TEST_CODE = "40504040001"
MID_TEST_CODE = "60001019906"
MID_TEST_PHONE = "+37200000766"


async def drive(provider, code: str, **start_kwargs) -> bool:
    name = provider.key
    print(f"\n=== {name} — live demo authentication ===")
    print(f"endpoint: {provider.base_url}")
    t0 = time.monotonic()
    try:
        start = await provider.start_authentication(code, **start_kwargs)
    except EidError as exc:
        print(f"START FAILED: {exc.code}: {exc.detail}")
        return False
    print(f"session started (verification code shown to user: {start.verification_code})")
    for attempt in range(1, 31):
        try:
            assertion = await provider.check_session(start.state)
        except EidError as exc:
            print(f"poll {attempt}: TERMINAL {exc.code}: {exc.detail}")
            return False
        if assertion is None:
            print(f"poll {attempt}: RUNNING")
            await asyncio.sleep(1)
            continue
        dt = time.monotonic() - t0
        print(f"poll {attempt}: COMPLETE/OK in {dt:.1f}s")
        print("  certificate chain: VALID (pinned SK demo CA)")
        print("  signature over our challenge: VALID")
        print(f"  identity: PNO{assertion.country}-{assertion.personal_code} "
              f"{assertion.given_name} {assertion.surname}")
        if assertion.document_number:
            print(f"  document: {assertion.document_number}")
        return True
    print("gave up after 30 polls")
    return False


async def main() -> int:
    ok_sid = await drive(SmartIdProvider(), SMARTID_TEST_CODE)
    ok_mid = await drive(MobileIdProvider(), MID_TEST_CODE, phone_number=MID_TEST_PHONE)
    print("\n=== summary ===")
    print(f"Smart-ID  demo end-to-end: {'PASS' if ok_sid else 'FAIL'}")
    print(f"Mobile-ID demo end-to-end: {'PASS' if ok_mid else 'FAIL'}")
    return 0 if (ok_sid and ok_mid) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
