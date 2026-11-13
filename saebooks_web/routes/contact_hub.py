"""Contact page as a working hub (gaps P1-75, P1-76, P2-26).

* ``GET  /contacts/{id}/hub`` — HTMX fragment under the contact details:
  people at the contact, dated notes and the emails sent about the
  contact's documents (with bounced / delivered / opened state). Loaded on
  its own so the contact page renders even if this part of the engine is
  slow or not yet migrated.
* ``POST /contacts/{id}/people``                 — add a person
* ``POST /contacts/{id}/people/{person_id}/remove`` — stop using a person
  (kept for history)
* ``POST /contacts/{id}/notes``                  — add a dated note
* ``POST /contacts/{id}/notes/{note_id}/delete`` — delete a note

All writes go through the engine's ``/api/v1/contact-records`` routes and
redirect back to the contact page (Post-Redirect-Get). The action row and
tap-to-call live in the ``b15/contact_actions.html`` partial included by
``contacts/detail.html``.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.i18n import gettext as _
from saebooks_web.module_gate import ModuleUnavailable

router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)

#: Engine delivery state -> (label, tone). Worst news is the loudest.
EMAIL_STATES = {
    "bounced": ("Bounced", "neg"),
    "complained": ("Marked as spam", "neg"),
    "failed": ("Not sent", "neg"),
    "not_sent": ("Draft, not sent", "warm"),
    "queued": ("Waiting to send", "warm"),
    "sent": ("Sent", "ink-3"),
    "delivered": ("Delivered", "pos"),
    "opened": ("Opened", "pos"),
}

_DOC_URLS = {
    "invoice": "/invoices/{id}",
    "quote": "/quotes/{id}",
    "credit_note": "/credit-notes/{id}",
    "bill": "/bills/{id}",
    "remittance": "/payments/{id}",
}


def _back(contact_id: uuid.UUID, anchor: str = "contact-hub") -> RedirectResponse:
    return RedirectResponse(url=f"/contacts/{contact_id}#{anchor}", status_code=303)


def _detail(resp) -> str:
    try:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list) and detail:
            return str(detail[0].get("msg", ""))
    except Exception:
        pass
    return _("Could not save (HTTP %(code)s).") % {"code": resp.status_code}


@router.get("/contacts/{contact_id}/hub", response_class=HTMLResponse, response_model=None)
async def contact_hub(request: Request, contact_id: uuid.UUID) -> HTMLResponse:
    if not request.session.get("api_token"):
        return HTMLResponse("", status_code=401)
    base = f"/api/v1/contact-records/{contact_id}"
    people: list[dict] = []
    notes: list[dict] = []
    emails: list[dict] = []
    unavailable = False
    try:
        async with api_client(request) as client:
            p = await client.get(f"{base}/people")
            n = await client.get(f"{base}/notes")
            c = await client.get(f"{base}/communications", params={"limit": 25})
        if p.is_success:
            people = p.json().get("items", [])
        if n.is_success:
            notes = n.json().get("items", [])
        if c.is_success:
            emails = c.json().get("items", [])
        unavailable = not (p.is_success and n.is_success)
    except ModuleUnavailable:
        unavailable = True
    for e in emails:
        label, tone = EMAIL_STATES.get(e.get("state", "sent"), ("Sent", "ink-3"))
        e["state_label"] = _(label)
        e["state_tone"] = tone
        tmpl = _DOC_URLS.get(e.get("doc_type", ""))
        e["doc_url"] = tmpl.format(id=e["doc_id"]) if tmpl else None
    return _TEMPLATES.TemplateResponse(
        request,
        "b15/contact_hub.html",
        {
            "contact_id": str(contact_id),
            "people": people,
            "notes": notes,
            "emails": emails,
            "unavailable": unavailable,
            "error": request.session.pop("hub_error", None),
        },
    )


@router.post("/contacts/{contact_id}/people", response_class=HTMLResponse, response_model=None)
async def add_person(request: Request, contact_id: uuid.UUID) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body = {
        "name": str(form.get("name") or "").strip(),
        "role": str(form.get("role") or "").strip() or None,
        "email": str(form.get("email") or "").strip() or None,
        "phone": str(form.get("phone") or "").strip() or None,
        "receives_documents": form.get("receives_documents") in ("1", "on", "true"),
    }
    if not body["name"]:
        request.session["hub_error"] = _("A name is required.")
        return _back(contact_id)
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/contact-records/{contact_id}/people", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        request.session["hub_error"] = _detail(resp)
    return _back(contact_id)


@router.post(
    "/contacts/{contact_id}/people/{person_id}/remove",
    response_class=HTMLResponse,
    response_model=None,
)
async def remove_person(
    request: Request, contact_id: uuid.UUID, person_id: uuid.UUID
) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/contact-records/{contact_id}/people/{person_id}")
    if not resp.is_success and resp.status_code != 404:
        request.session["hub_error"] = _detail(resp)
    return _back(contact_id)


@router.post("/contacts/{contact_id}/notes", response_class=HTMLResponse, response_model=None)
async def add_note(request: Request, contact_id: uuid.UUID) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    body: dict[str, str] = {"body": str(form.get("body") or "").strip()}
    note_date = str(form.get("note_date") or "").strip()
    if note_date:
        body["note_date"] = note_date
    if not body["body"]:
        request.session["hub_error"] = _("The note is empty.")
        return _back(contact_id)
    async with api_client(request) as client:
        resp = await client.post(f"/api/v1/contact-records/{contact_id}/notes", json=body)
    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        request.session["hub_error"] = _detail(resp)
    return _back(contact_id)


@router.post(
    "/contacts/{contact_id}/notes/{note_id}/delete",
    response_class=HTMLResponse,
    response_model=None,
)
async def delete_note(
    request: Request, contact_id: uuid.UUID, note_id: uuid.UUID
) -> RedirectResponse:
    if not request.session.get("api_token"):
        return RedirectResponse(url="/login", status_code=303)
    async with api_client(request) as client:
        resp = await client.delete(f"/api/v1/contact-records/{contact_id}/notes/{note_id}")
    if not resp.is_success and resp.status_code != 404:
        request.session["hub_error"] = _detail(resp)
    return _back(contact_id)
