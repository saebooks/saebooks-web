"""Contact form — public enquiry pages.

Routes
------
GET  /contact        — render the contact form; pre-selects topic from ?topic= query param.
POST /contact        — submit the form; proxies to the API, re-renders on error.
GET  /contact/thanks — thank-you page shown after successful submission.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.contact")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_VALID_TOPICS: frozenset[str] = frozenset({"general", "enterprise", "support"})


def _api_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.api_url, timeout=10.0)


# ---------------------------------------------------------------------------
# GET /contact
# ---------------------------------------------------------------------------


@router.get("/contact", response_class=HTMLResponse)
async def contact_page(
    request: Request,
    topic: str | None = None,
) -> HTMLResponse:
    safe_topic: str = topic if topic in _VALID_TOPICS else "general"
    return _TEMPLATES.TemplateResponse(
        request,
        "contact/form.html",
        {
            "error": None,
            "values": {"topic": safe_topic},
        },
    )


# ---------------------------------------------------------------------------
# POST /contact
# ---------------------------------------------------------------------------


@router.post("/contact", response_model=None)
async def contact_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    topic: str = Form(default="general"),
    message: str = Form(...),
    website: str | None = Form(default=None),
) -> HTMLResponse:
    safe_topic: str = topic if topic in _VALID_TOPICS else "general"
    payload = {
        "name": name.strip(),
        "email": email.strip(),
        "topic": safe_topic,
        "message": message,
    }
    # Pass honeypot field to API
    if website:
        payload["website"] = website

    try:
        async with _api_client() as client:
            resp = await client.post("/api/v1/contact/submit", json=payload)
    except httpx.RequestError:
        return _TEMPLATES.TemplateResponse(
            request,
            "contact/form.html",
            {
                "error": "Couldn't reach the server. Please try again in a moment.",
                "values": {"name": name, "email": email, "topic": safe_topic, "message": message},
            },
            status_code=502,
        )

    if resp.is_success:
        return RedirectResponse(url="/contact/thanks", status_code=303)

    # 422 validation errors or 429 rate limit
    if resp.status_code == 429:
        error_msg = "Too many submissions — please try again in an hour."
    else:
        try:
            detail = resp.json().get("detail", "")
            error_msg = str(detail) if detail else "Submission failed — please check your input and try again."
        except Exception:
            error_msg = "Submission failed — please try again."

    return _TEMPLATES.TemplateResponse(
        request,
        "contact/form.html",
        {
            "error": error_msg,
            "values": {"name": name, "email": email, "topic": safe_topic, "message": message},
        },
        status_code=resp.status_code if resp.status_code in (422, 429) else 502,
    )


# ---------------------------------------------------------------------------
# GET /contact/thanks
# ---------------------------------------------------------------------------


@router.get("/contact/thanks", response_class=HTMLResponse)
async def contact_thanks(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "contact/thanks.html",
        {},
    )


__all__ = ["router"]
