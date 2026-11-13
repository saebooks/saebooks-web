"""Publish-and-post for the document inbox (gap P2-23, 2026-11-13 sweep).

On a phone, a receipt took several screens to become a real expense:
publish as a draft, open the draft, press Post. The review page now has a
"Save and post" button. The engine still publishes a DRAFT exactly as
before (it never posts on publish — that stays a deliberate, separate
step); this helper then posts that draft straight away through the normal
``POST /api/v1/{expenses|bills}/{id}/post`` with its version, so every
posting rule (period locks, account checks) applies as usual.

If posting is refused, the draft is kept and the user lands on it with the
engine's reason, so nothing is lost. Credit notes are left as drafts:
applying a credit is a decision, not a one-tap action.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

from saebooks_web.i18n import gettext as _

_POSTABLE = {
    "EXPENSE": ("/api/v1/expenses/{id}", "/expenses/{id}", "expense"),
    "BILL": ("/api/v1/bills/{id}", "/bills/{id}", "bill"),
}

_DRAFT_ONLY = {
    "CREDIT_NOTE": "/credit-notes/{id}",
    "SUPPLIER_CREDIT_NOTE": "/supplier-credit-notes/{id}",
}


def _reason(resp) -> str:
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, list) and detail:
            return str(detail[0].get("msg", ""))
    except Exception:
        pass
    return _("HTTP %(code)s") % {"code": resp.status_code}


async def post_published_record(request: Request, client, publish_body: dict) -> RedirectResponse:
    record = publish_body.get("record") or {}
    kind = (record.get("kind") or "").upper()
    record_id = record.get("id", "")
    if kind not in _POSTABLE:
        url = _DRAFT_ONLY.get(kind, "/inbox").format(id=record_id)
        request.session["flash"] = _(
            "Saved as a draft %(kind)s. Credits are not posted automatically — check and post it here."
        ) % {"kind": kind.replace("_", " ").lower()}
        return RedirectResponse(url=url, status_code=303)

    api_path, page, label = _POSTABLE[kind]
    api_url = api_path.format(id=record_id)
    page_url = page.format(id=record_id)
    current = await client.get(api_url)
    if not current.is_success:
        request.session["flash"] = _(
            "Saved as a draft %(kind)s, but it could not be posted: %(why)s"
        ) % {"kind": _(label), "why": _reason(current)}
        return RedirectResponse(url=page_url, status_code=303)
    version = current.json().get("version")
    posted = await client.post(f"{api_url}/post", headers={"If-Match": str(version)})
    if posted.is_success:
        request.session["flash"] = _(
            "Posted the %(kind)s — the receipt is attached."
        ) % {"kind": _(label)}
    else:
        request.session["flash"] = _(
            "Saved as a draft %(kind)s, but it was not posted: %(why)s"
        ) % {"kind": _(label), "why": _reason(posted)}
    return RedirectResponse(url=page_url, status_code=303)
