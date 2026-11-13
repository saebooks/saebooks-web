"""Bulk Post / Void / Archive for the document lists (batch 3, gap P0-12).

Every engine transition route (post, void, archive-by-DELETE, quote send /
accept / decline / archive) requires ``If-Match: <version>`` and returns 428
without it; post and void also take an ``X-Idempotency-Key``. The list bulk
handlers sent neither, so select-all then Post reported a failure on every
row. :func:`run_bulk` fetches each row's current version, sends it with a
fresh idempotency key, and reports failures by document number in plain
words.

Used by the invoices, bills, credit notes, expenses, quotes and payments
lists; each keeps its own action table and redirect.
"""
from __future__ import annotations

import uuid
from typing import Any

# Which status a row must be in for each list action. DELETE on a POSTED
# bill / invoice / payment VOIDS it in the engine, so "archive" and
# "delete" must never reach a posted row; "void" must never reach a draft.
DEFAULT_RULES: dict[str, set[str]] = {
    "post": {"DRAFT"},
    "void": {"POSTED"},
    "archive": {"DRAFT"},
    "delete": {"DRAFT"},
}
_RULE_REASON = {
    "post": "only drafts can be posted",
    "void": "only posted items can be voided",
    "archive": "only drafts can be archived — void a posted one instead",
    "delete": "only drafts can be deleted — void a posted one instead",
}


def _error_text(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        return str(detail[0].get("msg", ""))
    if body.get("message"):
        return str(body["message"])
    return ""


async def run_one(
    client: Any,
    *,
    get_path: str,
    method: str,
    path: str,
    action: str,
    rules: dict[str, set[str]] | None = None,
) -> tuple[bool, str, str]:
    """Run one transition with the row's current version.

    Returns ``(ok, label, reason)`` — ``label`` is the document number when
    the row could be read, else the first 8 characters of its id.
    """
    label = get_path.rstrip("/").rsplit("/", 1)[-1][:8]
    cur = await client.get(get_path)
    if cur.status_code == 404:
        return False, label, "not found"
    if not cur.is_success:
        return False, label, f"could not be read (HTTP {cur.status_code})"
    doc = cur.json()
    label = str(doc.get("number") or doc.get("reference") or label)
    allowed = (DEFAULT_RULES if rules is None else rules).get(action)
    status = str(doc.get("status") or "").upper()
    if allowed and status and status not in allowed:
        return False, label, _RULE_REASON.get(action, f"not allowed while {status.lower()}")
    headers = {"If-Match": str(doc.get("version", ""))}
    if method == "POST":
        headers["X-Idempotency-Key"] = str(uuid.uuid4())
    resp = await client.request(method, path, headers=headers)
    if 200 <= resp.status_code < 300:
        return True, label, ""
    if resp.status_code == 409:
        return False, label, "changed by someone else — reload and try again"
    reason = _error_text(resp)
    return False, label, reason or f"HTTP {resp.status_code}"


async def run_bulk(
    client: Any,
    ids: list[str],
    *,
    method: str,
    path_tpl: str,
    action: str,
    get_tpl: str | None = None,
    rules: dict[str, set[str]] | None = None,
) -> tuple[int, list[str]]:
    """Run ``action`` on every id; returns ``(succeeded, failures)`` where
    each failure reads ``"<number> (<reason>)"``. One bad row never stops
    the batch."""
    if get_tpl is None:
        # "/api/v1/bills/{id}/post" -> "/api/v1/bills/{id}"
        get_tpl = path_tpl[: path_tpl.index("{id}") + len("{id}")]
    ok = 0
    failed: list[str] = []
    for row_id in ids:
        try:
            done, label, reason = await run_one(
                client,
                get_path=get_tpl.format(id=row_id),
                method=method,
                path=path_tpl.format(id=row_id),
                action=action,
                rules=rules,
            )
        except Exception as exc:  # transport error on one row
            failed.append(f"{row_id[:8]} (could not reach the server: {exc!s})")
            continue
        if done:
            ok += 1
        else:
            failed.append(f"{label} ({reason})")
    return ok, failed


def summary(action_label: str, ok: int, failed: list[str], noun: str) -> str:
    """Flash text: "Post: 3 bills done." or "Post: 2 done, 1 not done — …"."""
    if not failed:
        return f"{action_label}: {ok} {noun}{'' if ok == 1 else 's'} done."
    shown = "; ".join(failed[:5]) + (f" … and {len(failed) - 5} more" if len(failed) > 5 else "")
    return f"{action_label}: {ok} done, {len(failed)} not done — {shown}"
