"""Public-tier billing routes — Stripe Checkout entry + success page.

Companion to ``saebooks_web.routes.public_auth``.

* ``POST /billing/checkout`` — auth-required form submit; calls
  ``/api/v1/billing/checkout-session`` and 303-redirects the browser
  to the returned Stripe Checkout URL.
* ``GET  /billing/checkout-success`` — Stripe redirects here with
  ``?session_id=cs_…``.  We render a confirmation page; the actual
  edition+subscription state is set by the webhook (which races us
  but is usually faster).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.billing")

router = APIRouter(prefix="/billing")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _api_token(request: Request) -> str | None:
    return request.session.get("api_token")


@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, plan: str | None = None) -> Any:
    """Render the checkout landing page.

    After email verification the web layer redirects here with
    ?plan=business / ?plan=pro / ?plan=enterprise.
    If the user is not logged in they go to /login first (the ?next
    redirect brings them back). Otherwise we immediately auto-POST
    the Stripe Checkout session via a self-submitting form to avoid
    a visible "click to continue" step.
    """
    token = _api_token(request)
    if not token:
        return RedirectResponse(
            url=f"/login?next=/billing/checkout" + (f"?plan={plan}" if plan else ""),
            status_code=303,
        )
    _VALID_PLANS = {"business", "pro", "enterprise"}
    safe_plan = plan if plan in _VALID_PLANS else None
    if not safe_plan:
        # No plan — send straight to upgrade page
        return RedirectResponse(url="/billing/upgrade", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "billing/checkout_redirect.html",
        {"plan": safe_plan},
    )


@router.post("/checkout", response_model=None)
async def checkout(
    request: Request,
    edition: str = Form(...),
) -> Any:
    token = _api_token(request)
    if not token:
        return RedirectResponse(url="/login?next=/billing/upgrade", status_code=303)
    if edition not in {"business", "pro"}:
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": f"Unknown edition: {edition!r}"},
            status_code=400,
        )
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=15.0) as client:
            resp = await client.post(
                "/api/v1/billing/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
                json={"edition": edition},
            )
    except httpx.RequestError as exc:
        logger.error("billing/checkout: API unreachable: %s", exc)
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": "Couldn't reach the billing service. Try again in a moment."},
            status_code=502,
        )
    if resp.status_code == 401:
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 403:
        # email not verified
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {
                "message": "Verify your email before subscribing. Check your inbox or "
                "request a new link from /forgot-password."
            },
            status_code=403,
        )
    if not resp.is_success:
        try:
            detail = resp.json().get("detail") or resp.text
        except Exception:
            detail = resp.text
        logger.error("billing/checkout: API %d: %s", resp.status_code, detail)
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": str(detail) or "Couldn't start checkout. Please try again."},
            status_code=502,
        )
    url = resp.json().get("checkout_url")
    if not url:
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": "Billing returned an empty Checkout URL — please contact support."},
            status_code=502,
        )
    return RedirectResponse(url=url, status_code=303)


@router.get("/checkout-success", response_class=HTMLResponse)
async def checkout_success(
    request: Request,
    session_id: str | None = None,
) -> HTMLResponse:
    """Stripe success-redirect target.  We render an info page; the
    actual subscription state is applied by the webhook handler which
    races us by tens of milliseconds and usually wins."""
    return _TEMPLATES.TemplateResponse(
        request,
        "billing/checkout_success.html",
        {"session_id": session_id or ""},
    )
