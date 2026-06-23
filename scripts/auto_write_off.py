#!/usr/bin/env python3
"""Auto bad-debt write-off job — Phase 2 / Task 10.

Cron-invocable. For every company in the caller's tenant whose
``writeoff_mode == "auto"``, finds POSTED invoices past the company's
``writeoff_threshold_days`` with a balance still owing and writes each one
off via the engine endpoint ``POST /api/v1/invoices/{id}/write-off``.

Idempotency
-----------
Only POSTED invoices are candidates (see bad_debt_logic.CANDIDATE_STATUS).
Once written off an invoice becomes WRITTEN_OFF and can never re-enter the
candidate set, so re-running the job is safe. As a belt-and-braces measure
a 409 from the engine ("already written off") is treated as a skip, not a
failure — covers the race where two runs overlap.

The web app NEVER posts the journal entry itself — the engine owns the
ledger. This job is purely an orchestrator over the existing engine API.

Environment
-----------
SAEBOOKS_API_URL    Base URL of the engine API (default http://localhost:8042)
SAEBOOKS_API_TOKEN  Bearer token for ONE tenant (required). The job is
                    tenant-scoped: run it once per tenant with that tenant's
                    token. Issue tokens at /admin/api-tokens.

Trigger (cron, AEST)
--------------------
Run nightly at 02:15. Example crontab line on the host that can reach the
engine (8042):

    15 2 * * *  SAEBOOKS_API_URL=http://localhost:8042 \
                SAEBOOKS_API_TOKEN=@/etc/saebooks/auto_writeoff.token \
                /home/sauer/projects/saebooks-web/.venv/bin/python \
                /home/sauer/projects/saebooks-web/scripts/auto_write_off.py \
                >> /var/log/saebooks/auto_writeoff.log 2>&1

(``SAEBOOKS_API_TOKEN`` accepts a literal token, or ``@/path`` to read the
token from a file so it never appears in the process table / crontab.)

Exit codes
----------
0  success (including "no candidates" / all-skipped)
1  configuration error (missing token) or an unrecoverable API failure
2  one or more individual write-offs failed (non-409); details on stderr
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

import httpx

# Make the shared logic importable when run as a bare script (no package install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saebooks_web.bad_debt_logic import CANDIDATE_STATUS, candidates  # noqa: E402

_DEFAULT_API_URL = "http://localhost:8042"


def _resolve_token() -> str | None:
    """Return the API token, supporting ``@/path/to/file`` indirection."""
    raw = os.environ.get("SAEBOOKS_API_TOKEN", "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        path = raw[1:]
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            print(f"ERROR: cannot read token file {path}: {exc}", file=sys.stderr)
            return None
    return raw


def _log(msg: str) -> None:
    ts = date.today().isoformat()
    print(f"[auto-write-off {ts}] {msg}", flush=True)


async def _list_auto_companies(client: httpx.AsyncClient) -> list[dict]:
    """Return tenant companies with writeoff_mode == 'auto' (non-archived)."""
    resp = await client.get("/api/v1/companies", params={"limit": 500, "offset": 0})
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [
        c
        for c in items
        if (c.get("writeoff_mode") or "review") == "auto"
        and c.get("archived_at") is None
    ]


async def _company_candidates(
    client: httpx.AsyncClient, company: dict, today: date
) -> list[dict]:
    """Fetch POSTED invoices for one company and return write-off candidates."""
    company_id = str(company["id"])
    threshold = int(company.get("writeoff_threshold_days") or 90)
    resp = await client.get(
        "/api/v1/invoices",
        params={"status": CANDIDATE_STATUS, "page": 1, "page_size": 500},
        headers={"X-Company-Id": company_id},
    )
    resp.raise_for_status()
    invoices = resp.json().get("items", [])
    return candidates(invoices, threshold, today)


async def _write_off_one(
    client: httpx.AsyncClient, company_id: str, inv: dict
) -> str:
    """Write off a single invoice. Returns one of: 'ok' | 'skip' | 'fail'."""
    inv_id = str(inv["id"])
    resp = await client.post(
        f"/api/v1/invoices/{inv_id}/write-off",
        json={"reason": "Auto write-off: balance unpaid past threshold"},
        headers={"X-Company-Id": company_id},
    )
    if resp.status_code == 200:
        _log(f"  wrote off {inv.get('number') or inv_id} "
             f"(${inv.get('_balance')}, {inv.get('_age_days')}d overdue)")
        return "ok"
    if resp.status_code == 409:
        # Already written off / nothing owed — idempotent skip.
        _log(f"  skip {inv.get('number') or inv_id}: {resp.text.strip()[:120]}")
        return "skip"
    _log(f"  FAIL {inv.get('number') or inv_id}: HTTP {resp.status_code} "
         f"{resp.text.strip()[:160]}")
    return "fail"


async def run() -> int:
    token = _resolve_token()
    if not token:
        print("ERROR: SAEBOOKS_API_TOKEN is required (literal or @/path).",
              file=sys.stderr)
        return 1

    api_url = os.environ.get("SAEBOOKS_API_URL", _DEFAULT_API_URL).rstrip("/")
    today = date.today()

    totals = {"ok": 0, "skip": 0, "fail": 0}

    async with httpx.AsyncClient(
        base_url=api_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        try:
            companies = await _list_auto_companies(client)
        except httpx.HTTPError as exc:
            print(f"ERROR: failed to list companies: {exc}", file=sys.stderr)
            return 1

        _log(f"{len(companies)} company(ies) in auto mode")
        if not companies:
            _log("nothing to do")
            return 0

        for company in companies:
            cid = str(company["id"])
            name = company.get("name", cid)
            try:
                cands = await _company_candidates(client, company, today)
            except httpx.HTTPError as exc:
                print(f"ERROR: {name}: failed to fetch invoices: {exc}",
                      file=sys.stderr)
                totals["fail"] += 1
                continue

            _log(f"{name}: {len(cands)} candidate(s) "
                 f"(threshold {company.get('writeoff_threshold_days', 90)}d)")
            for inv in cands:
                try:
                    outcome = await _write_off_one(client, cid, inv)
                except httpx.HTTPError as exc:
                    _log(f"  FAIL {inv.get('id')}: {exc}")
                    outcome = "fail"
                totals[outcome] += 1

    _log(f"done — wrote off {totals['ok']}, skipped {totals['skip']}, "
         f"failed {totals['fail']}")

    if totals["fail"]:
        return 2
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
