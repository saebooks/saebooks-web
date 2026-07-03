"""Pure bad-debt candidate logic — shared by the web screen and the auto job.

Kept dependency-free (no FastAPI / httpx) so it can be unit-tested in
isolation and imported by both ``routes/bad_debts.py`` (Task 9) and
``scripts/auto_write_off.py`` (Task 10) without pulling in the request stack.

A *candidate* is a POSTED invoice that still owes money and whose age past
its ``due_date`` exceeds the company's ``writeoff_threshold_days``.
"""
from __future__ import annotations

from datetime import date

# Only POSTED invoices are write-off candidates. DRAFT isn't owed yet;
# VOIDED / WRITTEN_OFF are terminal — this is what makes the auto job
# idempotent: an already-WRITTEN_OFF invoice can never re-enter the set.
CANDIDATE_STATUS = "POSTED"


def to_float(val: object) -> float:
    """Best-effort float coercion; 0.0 on None / unparseable input."""
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def age_days(due_date: object, today: date) -> int:
    """Days past ``due_date`` (negative if not yet due). 0 on bad input."""
    if not due_date:
        return 0
    try:
        d = date.fromisoformat(str(due_date)[:10])
    except ValueError:
        return 0
    return (today - d).days


def invoice_balance(inv: dict) -> float:
    """Outstanding balance = total - amount_paid (rounded to cents)."""
    return round(to_float(inv.get("total")) - to_float(inv.get("amount_paid")), 2)


def is_candidate(inv: dict, threshold_days: int, today: date) -> bool:
    """True iff POSTED, balance > 0, and age strictly > threshold."""
    if (inv.get("status") or "").upper() != CANDIDATE_STATUS:
        return False
    if invoice_balance(inv) <= 0:
        return False
    return age_days(inv.get("due_date"), today) > threshold_days


def candidates(invoices: list[dict], threshold_days: int, today: date) -> list[dict]:
    """Filter + annotate candidates with ``_balance`` and ``_age_days``.

    Returned oldest-first (longest overdue at the top).
    """
    out: list[dict] = []
    for inv in invoices:
        if not is_candidate(inv, threshold_days, today):
            continue
        annotated = dict(inv)
        annotated["_balance"] = invoice_balance(inv)
        annotated["_age_days"] = age_days(inv.get("due_date"), today)
        out.append(annotated)
    out.sort(key=lambda i: i["_age_days"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Recovery detection (Phase 2 / Task 11)
#
# When money comes in from a contact who has a WRITTEN_OFF invoice, the
# smart_prompt recovery_mode asks the operator whether to record it as a
# bad-debt recovery (engine POST /record-recovery). manual = never prompt;
# reopen = TODO stub (re-open the invoice instead).
# ---------------------------------------------------------------------------

WRITTEN_OFF_STATUS = "WRITTEN_OFF"


def should_prompt_recovery(recovery_mode: str | None) -> bool:
    """Only smart_prompt mode triggers the recovery prompt."""
    return (recovery_mode or "smart_prompt") == "smart_prompt"


def pick_recovery_invoice(written_off: list[dict]) -> dict | None:
    """Choose which written-off invoice a recovery most likely applies to.

    Heuristic: the most recently written-off (or, lacking that signal, the
    largest balance) is the best default. The operator can always pick a
    different one in the prompt. Returns None if the list is empty.
    """
    candidates_ = [i for i in written_off if (i.get("status") or "").upper() == WRITTEN_OFF_STATUS]
    if not candidates_:
        return None
    # Prefer the largest original total as the headline match.
    candidates_.sort(key=lambda i: to_float(i.get("total")), reverse=True)
    return candidates_[0]
