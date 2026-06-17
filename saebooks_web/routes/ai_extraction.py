"""AI document extraction web routes — D/55.

Proxy endpoints that forward a file upload to ``POST /api/v1/documents/extract``
and return HTMX fragments that pre-fill the bill or invoice create forms.

Route map
---------
GET  /bills/extract-document/probe     — feature-flag probe: 200 if enabled, 404 if not
GET  /invoices/extract-document/probe  — same for invoices
POST /bills/extract-document           — upload + proxy + fill fragment for bills
POST /invoices/extract-document        — upload + proxy + fill fragment for invoices

Auth guard: redirect to /login (303) if no session token.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_log = logging.getLogger(__name__)

_EXTRACT_API_PATH = "/api/v1/documents/extract"


def _require_auth(request: Request) -> str | None:
    """Return the token if present, else None (caller redirects)."""
    return request.session.get("api_token")


# ---------------------------------------------------------------------------
# Feature-flag probes — lightweight HEAD/GET so the template can decide
# whether to show the upload button.
# These are declared before the POST routes so that "literal" paths win over
# parameterised ones that exist in bills.py / invoices.py.
# ---------------------------------------------------------------------------


@router.get("/bills/extract-document/probe", response_class=HTMLResponse, response_model=None)
async def bills_extract_probe(request: Request) -> HTMLResponse | RedirectResponse:
    """Return 200 (feature on) or 404 (feature off / no key configured).

    The template uses ``hx-get`` on page load to toggle the upload button.
    This probe is auth-gated — an unauthenticated user won't see the button
    regardless.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(_EXTRACT_API_PATH)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # 404 → flag off; 405 (method not allowed) → feature exists but GET not supported
    # — treat anything other than flag-off (404) and unconfigured key (503) as enabled.
    if resp.status_code == 404:
        return HTMLResponse(content="", status_code=404)
    if resp.status_code == 503:
        return HTMLResponse(content="", status_code=503)

    # Feature is on — return the upload button fragment.
    return _TEMPLATES.TemplateResponse(
        request,
        "ai_extraction/_upload_button.html",
        {"form_context": "bill"},
    )


@router.get(
    "/invoices/extract-document/probe", response_class=HTMLResponse, response_model=None
)
async def invoices_extract_probe(request: Request) -> HTMLResponse | RedirectResponse:
    """Return 200 (feature on) or 404 (feature off / no key configured)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(_EXTRACT_API_PATH)

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    if resp.status_code == 404:
        return HTMLResponse(content="", status_code=404)
    if resp.status_code == 503:
        return HTMLResponse(content="", status_code=503)

    return _TEMPLATES.TemplateResponse(
        request,
        "ai_extraction/_upload_button.html",
        {"form_context": "invoice"},
    )


# ---------------------------------------------------------------------------
# POST /bills/extract-document
# ---------------------------------------------------------------------------


@router.post("/bills/extract-document", response_class=HTMLResponse, response_model=None)
async def bills_extract_document(request: Request) -> HTMLResponse | RedirectResponse:
    """Proxy a file upload to the extraction API and return a fill fragment.

    On success the fragment sets form field values via ``hx-swap-oob`` so the
    existing bill create form is pre-filled without a page reload.

    Status codes returned to the browser:
    - 200 — fragment rendered (high or low confidence)
    - 404 — feature flag not enabled (button should have been hidden)
    - 503 — API key not configured
    - 400 — no file provided / other client error
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return await _do_extract(request, form_context="bill")


# ---------------------------------------------------------------------------
# POST /invoices/extract-document
# ---------------------------------------------------------------------------


@router.post("/invoices/extract-document", response_class=HTMLResponse, response_model=None)
async def invoices_extract_document(request: Request) -> HTMLResponse | RedirectResponse:
    """Proxy a file upload to the extraction API and return a fill fragment."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    return await _do_extract(request, form_context="invoice")


# ---------------------------------------------------------------------------
# Shared extraction logic
# ---------------------------------------------------------------------------


async def _do_extract(request: Request, form_context: str) -> HTMLResponse:
    """Forward the uploaded file to the API and render the fill fragment."""
    form_data = await request.form()
    # Multipart routes are exempt from the body-parsing CSRFMiddleware; we
    # have to verify the token explicitly after parsing the form.  The
    # template includes {{ csrf_input(request) }} so the field is present.
    from saebooks_web.security import verify_csrf_form
    await verify_csrf_form(request)
    file_field = form_data.get("document")

    if not hasattr(file_field, "read"):
        return _TEMPLATES.TemplateResponse(
            request,
            "ai_extraction/_result_fragment.html",
            {
                "error": "No file provided.",
                "extraction": None,
                "form_context": form_context,
                "low_confidence": False,
            },
            status_code=400,
        )

    content = await file_field.read()  # type: ignore[union-attr]
    filename = getattr(file_field, "filename", "document") or "document"
    content_type = getattr(file_field, "content_type", "application/octet-stream") or "application/octet-stream"

    async with api_client(request) as client:
        resp = await client.post(
            _EXTRACT_API_PATH,
            files={"file": (filename, content, content_type)},
        )

    if resp.status_code == 401:
        request.session.clear()
        # Return an inline error rather than redirecting — this is an HTMX call.
        return _TEMPLATES.TemplateResponse(
            request,
            "ai_extraction/_result_fragment.html",
            {
                "error": "Session expired — please reload the page.",
                "extraction": None,
                "form_context": form_context,
                "low_confidence": False,
            },
            status_code=401,
        )

    if resp.status_code == 404:
        return _TEMPLATES.TemplateResponse(
            request,
            "ai_extraction/_result_fragment.html",
            {
                "error": "AI extraction is not enabled on this instance.",
                "extraction": None,
                "form_context": form_context,
                "low_confidence": False,
            },
            status_code=404,
        )

    if resp.status_code == 503:
        return _TEMPLATES.TemplateResponse(
            request,
            "ai_extraction/_result_fragment.html",
            {
                "error": "AI extraction is not available — the API key has not been configured.",
                "extraction": None,
                "form_context": form_context,
                "low_confidence": False,
            },
            status_code=503,
        )

    if not resp.is_success:
        try:
            detail = resp.json().get("detail", f"Extraction failed: HTTP {resp.status_code}")
        except Exception:
            detail = f"Extraction failed: HTTP {resp.status_code}"
        return _TEMPLATES.TemplateResponse(
            request,
            "ai_extraction/_result_fragment.html",
            {
                "error": str(detail),
                "extraction": None,
                "form_context": form_context,
                "low_confidence": False,
            },
            status_code=resp.status_code,
        )

    extraction = resp.json()
    confidence = float(extraction.get("extraction_confidence", 1.0))
    low_confidence = confidence < 0.7

    return _TEMPLATES.TemplateResponse(
        request,
        "ai_extraction/_result_fragment.html",
        {
            "error": None,
            "extraction": extraction,
            "form_context": form_context,
            "low_confidence": low_confidence,
            "confidence_pct": round(confidence * 100),
        },
    )
