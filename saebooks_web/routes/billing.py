"""Public-tier billing routes — Stripe Checkout entry + success page.

Companion to ``saebooks_web.routes.public_auth``.

* ``GET  /billing/checkout?plan=X`` — auth-required; renders the plan
  confirmation interstitial (confirm_plan.html) so the user explicitly
  reviews what they're about to pay for before hitting Stripe.
  Unauthenticated visitors are redirected to /login?next=... first, so
  account creation is always required before reaching Stripe.
* ``POST /billing/checkout`` — form submit from the confirm-plan page;
  takes ``edition`` and ``period`` from the form, calls
  ``/api/v1/billing/checkout-session``, and 303-redirects the browser
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


_PLAN_META: dict[str, dict[str, Any]] = {
    "business": {
        "label": "Business",
        "tagline": "For sole traders and small teams who lodge BAS themselves.",
        "price_monthly": "49",
        "price_yearly": "490",
        "features": [
            "Everything in Community",
            "Bank feeds (SISS/ACSISS)",
            "ABR lookup, in-app",
            "BAS e-lodgement",
            "Fixed asset register",
            "Period locks",
            "Email support, business hours",
        ],
        "footer": "Up to 3 users. Single company. Cancel anytime.",
    },
    "pro": {
        "label": "Pro",
        "tagline": "For bookkeepers, growing teams, and anyone running payroll.",
        "price_monthly": "99",
        "price_yearly": "990",
        "features": [
            "Everything in Business",
            "Multi-company / intercompany",
            "STP Phase 2 payroll",
            "FX revaluation",
            "Open Journal / Hybrid audit modes",
            "Signed LTS releases",
            "Priority email support",
        ],
        "footer": "Unlimited users. Up to 10 companies. Cancel anytime.",
    },
    "enterprise": {
        "label": "Enterprise",
        "tagline": "Scoped per engagement. Contact us for pricing.",
        "price_monthly": "—",
        "price_yearly": "—",
        "features": [
            "Everything in Pro",
            "Custom SLA",
            "Dedicated support",
            "On-premise deployment options",
        ],
        "footer": "Quoted. Not subscribed.",
    },
}


@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, plan: str | None = None) -> Any:
    """Render the plan confirmation interstitial.

    After email verification the web layer redirects here with
    ?plan=business / ?plan=pro / ?plan=enterprise.
    If the user is not logged in they are redirected to /login first —
    the ?next param brings them back, so account creation is always
    required before a visitor can reach Stripe.

    Once authenticated the user sees a branded plan-summary card with an
    explicit "Continue to payment" button.  The old auto-submitting
    checkout_redirect.html is no longer used for this flow.
    """
    token = _api_token(request)
    if not token:
        next_url = "/billing/checkout" + (f"?plan={plan}" if plan else "")
        return RedirectResponse(
            url=f"/login?next={next_url}",
            status_code=303,
        )
    _VALID_PLANS = {"business", "pro", "enterprise"}
    safe_plan = plan if plan in _VALID_PLANS else None
    if not safe_plan:
        # No plan — send to upgrade/pricing page
        return RedirectResponse(url="/billing/upgrade", status_code=303)
    meta = _PLAN_META[safe_plan]
    return _TEMPLATES.TemplateResponse(
        request,
        "billing/confirm_plan.html",
        {
            "plan": safe_plan,
            "plan_label": meta["label"],
            "plan_tagline": meta["tagline"],
            "plan_price_monthly": meta["price_monthly"],
            "plan_price_yearly": meta["price_yearly"],
            "plan_features": meta["features"],
            "plan_footer": meta["footer"],
        },
    )


@router.post("/checkout", response_model=None)
async def checkout(
    request: Request,
    edition: str = Form(...),
    period: str = Form("month"),
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
    if period not in {"month", "year"}:
        # Belt-and-braces — the radio in confirm_plan.html only emits
        # 'month' or 'year', but a hand-crafted POST could pass anything.
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": f"Unknown billing period: {period!r}"},
            status_code=400,
        )
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=15.0) as client:
            resp = await client.post(
                "/api/v1/billing/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
                json={"edition": edition, "period": period},
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


@router.post("/manage", response_model=None)
async def manage_billing(request: Request) -> Any:
    """Redirect the authenticated user to the Stripe Customer Portal."""
    token = _api_token(request)
    if not token:
        return RedirectResponse(url="/login?next=/admin/license", status_code=303)
    try:
        async with httpx.AsyncClient(base_url=settings.api_url, timeout=15.0) as client:
            resp = await client.post(
                "/api/v1/billing/portal-session",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as exc:
        logger.error("billing/manage: API unreachable: %s", exc)
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": "Couldn't reach the billing service. Try again in a moment."},
            status_code=502,
        )
    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": "No active subscription found. Subscribe first to manage billing."},
            status_code=404,
        )
    if resp.status_code == 401:
        return RedirectResponse(url="/login", status_code=303)
    if not resp.is_success:
        detail = resp.json().get("detail") or resp.text
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": str(detail) or "Couldn't open billing portal. Please try again."},
            status_code=502,
        )
    url = resp.json().get("portal_url")
    if not url:
        return _TEMPLATES.TemplateResponse(
            request,
            "billing/checkout_error.html",
            {"message": "Billing returned an empty portal URL — contact support."},
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
