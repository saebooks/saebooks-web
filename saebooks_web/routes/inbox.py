"""Document Inbox web routes — issue #33 phase 1 (spec §4 upload, §6 review & publish).

Thin proxy over the engine's ``/api/v1/inbox`` surface. All inbox logic
(state machine, dedupe, extraction, publish) lives engine-side; this layer
renders the review UI and forwards form submissions.

GET  /inbox                          — list page (status tabs, upload zone, HTMX-aware)
GET  /inbox/_badge                   — nav badge fragment (lazy HTMX load)
POST /inbox/upload                   — multipart capture → engine, redirect to review
GET  /inbox/{id}                     — review page (split pane: preview + coding form)
GET  /inbox/{id}/file                — stream the source blob inline (embed/img src)
POST /inbox/{id}/review              — action=save → PATCH override; action=publish →
                                       PATCH then publish as DRAFT record
POST /inbox/{id}/extract             — manual extraction retry
POST /inbox/{id}/reject              — reject with reason + note
POST /inbox/{id}/quick-contact       — inline supplier quick-create from vendor_name
                                       (HTMX swap of the contact select fragment)

Engine gate semantics surfaced here:
* 404 from the engine — FLAG_DOCUMENT_INBOX off → "not enabled" page.
* 503 from the engine — vault not configured → disabled banner page.
* 409 — optimistic-lock conflict → reload banner on the review page.

Statuses are UPPERCASE TEXT end to end; publish creates DRAFT records only.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.api_client import api_client
from saebooks_web.form_helpers import parse_lines as _parse_lines

logger = logging.getLogger("saebooks_web.routes.inbox")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Mirror of the engine's inbox status vocabulary (TEXT, UPPERCASE).
_OPEN_STATUSES = ("RECEIVED", "EXTRACTING", "NEEDS_REVIEW", "READY", "FAILED")
_ALL_STATUSES = (*_OPEN_STATUSES, "PUBLISHED", "REJECTED", "DUPLICATE")
_REJECT_REASONS = ("DUPLICATE", "NOT_A_DOCUMENT", "PERSONAL", "OTHER")
_RECORD_KINDS = ("EXPENSE", "BILL", "CREDIT_NOTE")


def _require_auth(request: Request) -> str | None:
    return request.session.get("api_token")


def _age_label(created_at: str | None) -> str:
    """Humanised age from an ISO timestamp — '4m', '3h', '2d'."""
    if not created_at:
        return ""
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
    except ValueError:
        return ""
    seconds = max(0, int((datetime.now(UTC) - created).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _merged_extract(doc: dict) -> dict[str, Any]:
    """Reviewer-effective view — extract shallow-overlaid by override.

    Mirrors the engine's ``services/document_inbox.merged_extract``:
    override keys win; ``line_items`` in the override replaces the
    extracted list wholesale.
    """
    merged: dict[str, Any] = dict(doc.get("extract") or {})
    merged.update(doc.get("extraction_override") or {})
    return merged


def _field_source(doc: dict, key: str) -> str:
    """Provenance of a prefilled field: 'edited' | 'rule' | 'model' | ''.

    Rule suggestions (``suggested_*``) arrive with phase 2 supplier
    rules — supported here already so the visual distinction lights up
    without a template change when the engine starts sending them.
    """
    override = doc.get("extraction_override") or {}
    if key in override:
        return "edited"
    if doc.get(f"suggested_{key}") not in (None, ""):
        return "rule"
    if (doc.get("extract") or {}).get(key) not in (None, ""):
        return "model"
    return ""


def _normalise_lines(merged: dict[str, Any]) -> list[dict[str, Any]]:
    """Line items for the editable table.

    The model emits ``{description, qty, unit_price, amount, tax_code}``;
    reviewer overrides carry ``{description, quantity, unit_price,
    account_id, tax_code_id}`` (line amount = unit_price convention).
    Normalise both shapes into the form's vocabulary.
    """
    raw = merged.get("line_items")
    if not isinstance(raw, list):
        return []
    lines: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        lines.append(
            {
                "description": item.get("description") or "",
                "quantity": item.get("quantity") or item.get("qty") or "1",
                "unit_price": item.get("unit_price") or item.get("amount") or "",
                "account_id": item.get("account_id") or "",
                "tax_code_id": item.get("tax_code_id") or "",
            }
        )
    return lines


async def _fetch_dropdowns(client) -> dict[str, list[dict]]:
    """Contacts (SUPPLIER+BOTH), expense accounts, payment accounts
    (ASSET+LIABILITY — the bank/card/cash credit pool), tax codes and
    companies for the review coding form. Mirrors expenses.py."""
    out: dict[str, list[dict]] = {
        "contacts": [],
        "expense_accounts": [],
        "payment_accounts": [],
        "tax_codes": [],
        "companies": [],
    }

    for ctype in ("SUPPLIER", "BOTH"):
        r = await client.get(
            "/api/v1/contacts", params={"type": ctype, "limit": 500, "offset": 0}
        )
        if r.is_success:
            out["contacts"].extend(r.json().get("items", []))

    e_resp = await client.get(
        "/api/v1/accounts", params={"account_type": "EXPENSE", "limit": 500, "offset": 0}
    )
    if e_resp.is_success:
        out["expense_accounts"] = e_resp.json().get("items", [])

    for atype in ("ASSET", "LIABILITY"):
        r = await client.get(
            "/api/v1/accounts", params={"account_type": atype, "limit": 500, "offset": 0}
        )
        if r.is_success:
            out["payment_accounts"].extend(r.json().get("items", []))
    out["payment_accounts"].sort(key=lambda a: a.get("code", ""))

    t_resp = await client.get("/api/v1/tax_codes", params={"page_size": 500})
    if t_resp.is_success:
        out["tax_codes"] = t_resp.json().get("items", [])

    c_resp = await client.get("/api/v1/companies", params={"limit": 100, "offset": 0})
    if c_resp.is_success:
        payload = c_resp.json()
        out["companies"] = (
            payload.get("items", []) if isinstance(payload, dict) else payload
        )

    return out


def _gate_page(request: Request, status_code: int) -> HTMLResponse:
    """Render the feature-off (404) / vault-off (503) explainer page."""
    return _TEMPLATES.TemplateResponse(
        request,
        "inbox/disabled.html",
        {"reason": "flag" if status_code == 404 else "vault"},
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/inbox", response_class=HTMLResponse, response_model=None)
async def inbox_list(
    request: Request,
    status: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    status = (status or "").upper()
    if status not in _ALL_STATUSES:
        status = ""  # default tab: all open (engine excludes terminal states)

    page_size = max(1, min(limit, 200))
    page = (offset // page_size) + 1 if page_size > 0 else 1
    params: dict[str, object] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if source:
        params["source"] = source.upper()

    error: str | None = None
    docs: list[dict] = []
    total = 0
    stats: dict[str, Any] = {}

    async with api_client(request) as client:
        resp = await client.get("/api/v1/inbox/documents", params=params)
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code in (404, 503):
            return _gate_page(request, resp.status_code)
        if resp.is_success:
            payload = resp.json()
            docs = payload.get("items", [])
            total = payload.get("total", len(docs))
        else:
            error = f"API error: HTTP {resp.status_code}"

        s_resp = await client.get("/api/v1/inbox/stats")
        if s_resp.is_success:
            stats = s_resp.json()

    for doc in docs:
        merged = _merged_extract(doc)
        doc["_vendor"] = merged.get("vendor_name") or ""
        doc["_date"] = merged.get("date") or ""
        doc["_total"] = merged.get("total") or ""
        doc["_age"] = _age_label(doc.get("created_at"))

    flash = request.session.pop("flash", None)
    ctx = {
        "docs": docs,
        "total": total,
        "stats": stats,
        "error": error,
        "flash": flash,
        "filter_status": status,
        "filter_source": source or "",
        "limit": page_size,
        "offset": offset,
        "open_statuses": _OPEN_STATUSES,
    }

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "inbox/_table.html" if is_htmx else "inbox/list.html"
    return _TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Nav badge — lazy fragment so every page load doesn't block on the stats call
# ---------------------------------------------------------------------------


@router.get("/inbox/_badge", response_class=HTMLResponse, response_model=None)
async def inbox_badge(request: Request) -> HTMLResponse:
    if not _require_auth(request):
        return HTMLResponse("")

    try:
        async with api_client(request) as client:
            resp = await client.get("/api/v1/inbox/stats")
    except Exception:  # transport error — badge is cosmetic, never breaks nav
        return HTMLResponse("")

    if not resp.is_success:
        return HTMLResponse("")  # 404 flag-off / 503 vault-off → no badge

    stats = resp.json()
    count = sum(
        int(stats.get(key) or 0)
        for key in ("RECEIVED", "NEEDS_REVIEW", "READY", "FAILED")
    )
    if count <= 0:
        return HTMLResponse("")
    return HTMLResponse(
        '<span class="ml-auto inline-flex items-center justify-center rounded-full '
        'px-1.5 min-w-[1.25rem] h-5 text-[10px] font-semibold text-white" '
        f'style="background: var(--sae);">{count}</span>'
    )


# ---------------------------------------------------------------------------
# Upload — one multipart surface (desktop drag-drop + mobile camera)
# ---------------------------------------------------------------------------


@router.post("/inbox/upload", response_class=HTMLResponse, response_model=None)
async def inbox_upload(request: Request) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # Multipart bodies bypass the CSRF middleware — verify explicitly
    # (ai_extraction.py precedent).
    from saebooks_web.security import verify_csrf_form

    await verify_csrf_form(request)

    form = await request.form()
    file = form.get("file")
    if file is None or isinstance(file, str) or not getattr(file, "filename", None):
        request.session["flash"] = "No file selected."
        return RedirectResponse(url="/inbox", status_code=303)

    payload = await file.read()
    if not payload:
        request.session["flash"] = "The selected file is empty."
        return RedirectResponse(url="/inbox", status_code=303)

    data: dict[str, str] = {}
    company_id = str(form.get("company_id") or "").strip()
    if company_id:
        data["company_id"] = company_id

    async with api_client(request) as client:
        resp = await client.post(
            "/api/v1/inbox/documents",
            data=data,
            files={
                "file": (
                    file.filename or "upload",
                    payload,
                    file.content_type or "application/octet-stream",
                )
            },
            # Synchronous in-request extraction can take single-digit
            # seconds on a 10 MiB document — give it headroom beyond the
            # default 10 s client timeout.
            timeout=60.0,
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code in (404, 503):
        return _gate_page(request, resp.status_code)

    if resp.is_success:
        doc = resp.json()
        if doc.get("duplicate"):
            request.session["flash"] = (
                f"“{doc.get('filename', 'document')}” is already in the "
                "inbox — showing the existing document."
            )
        return RedirectResponse(url=f"/inbox/{doc['id']}", status_code=303)

    detail = f"Upload failed (HTTP {resp.status_code})"
    try:
        body_detail = resp.json().get("detail")
        if isinstance(body_detail, str):
            detail = body_detail
        elif isinstance(body_detail, list) and body_detail:
            detail = body_detail[0].get("msg", detail)
    except Exception:
        pass
    request.session["flash"] = detail
    return RedirectResponse(url="/inbox", status_code=303)


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------


async def _render_review(
    request: Request,
    client,
    doc: dict,
    *,
    errors: dict[str, str] | None = None,
    conflict: bool = False,
    status_code: int = 200,
    form_values: dict[str, str] | None = None,
) -> HTMLResponse:
    dropdowns = await _fetch_dropdowns(client)
    merged = _merged_extract(doc)
    lines = _normalise_lines(merged)
    if not lines:
        lines = [{"description": "", "quantity": "1", "unit_price": "",
                  "account_id": "", "tax_code_id": ""}]

    provenance = {
        key: _field_source(doc, key)
        for key in (
            "vendor_name", "contact_id", "date", "invoice_number",
            "total", "line_items", "payment_account_id",
        )
    }

    is_image = (doc.get("mime") or "").startswith("image/")
    flash = request.session.pop("flash", None)

    return _TEMPLATES.TemplateResponse(
        request,
        "inbox/review.html",
        {
            "doc": doc,
            "merged": merged,
            "lines": lines,
            "line_count": len(lines),
            "provenance": provenance,
            "is_image": is_image,
            "errors": errors or {},
            "conflict": conflict,
            "flash": flash,
            "form_values": form_values or {},
            "idempotency_key": str(uuid.uuid4()),
            "record_kinds": _RECORD_KINDS,
            "reject_reasons": _REJECT_REASONS,
            "reviewable": doc.get("status")
            in ("NEEDS_REVIEW", "READY", "FAILED", "RECEIVED"),
            **dropdowns,
        },
        status_code=status_code,
    )


@router.get("/inbox/{document_id}", response_class=HTMLResponse, response_model=None)
async def inbox_review(
    request: Request, document_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.get(f"/api/v1/inbox/documents/{document_id}")
        if resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if resp.status_code == 404:
            return HTMLResponse("Inbox document not found", status_code=404)
        if resp.status_code == 503:
            return _gate_page(request, 503)
        if not resp.is_success:
            request.session["flash"] = f"API error: HTTP {resp.status_code}"
            return RedirectResponse(url="/inbox", status_code=303)
        doc = resp.json()
        return await _render_review(request, client, doc)


@router.get("/inbox/{document_id}/file", response_model=None)
async def inbox_file(
    request: Request, document_id: uuid.UUID
) -> StreamingResponse | RedirectResponse | HTMLResponse:
    """Stream the source blob through this web layer for the preview pane
    (``<embed>``/``<img> src``) — bytes flow engine → web → browser, no
    presigned URLs, Content-Disposition inline."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    # Metadata first so headers are correct before the stream starts
    # (attachments.py download precedent).
    async with api_client(request) as client:
        meta_resp = await client.get(f"/api/v1/inbox/documents/{document_id}")
    if not meta_resp.is_success:
        return HTMLResponse("Document not available", status_code=404)
    meta = meta_resp.json()
    mime = meta.get("mime") or "application/octet-stream"
    filename = meta.get("filename") or "document"

    async def _relay():
        async with (
            api_client(request) as client,
            client.stream(
                "GET",
                f"/api/v1/inbox/documents/{document_id}/download",
                timeout=60.0,
            ) as api_resp,
        ):
            if not api_resp.is_success:
                logger.warning(
                    "inbox file relay: upstream %s for document %s",
                    api_resp.status_code, document_id,
                )
                return
            async for chunk in api_resp.aiter_bytes(chunk_size=65536):
                yield chunk

    return StreamingResponse(
        _relay(),
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Save / publish — one form, two actions
# ---------------------------------------------------------------------------


def _override_from_form(form: dict[str, str]) -> dict[str, Any]:
    """Build the ``extraction_override`` payload from the coding form.

    Reviewer edits live here, never in ``extract``. Line items carry
    the coding UUIDs (account/tax) the engine's READY completeness
    check looks for; blank optional keys are omitted.
    """
    override: dict[str, Any] = {}
    for key in ("vendor_name", "contact_id", "date", "invoice_number",
                "total", "payment_account_id"):
        val = (form.get(key) or "").strip()
        if val:
            override[key] = val

    lines = _parse_lines(form)
    if lines:
        override["line_items"] = lines
    return override


async def _patch_override(
    client, document_id: uuid.UUID, version: int, override: dict[str, Any],
    company_id: str | None,
):
    body: dict[str, Any] = {"version": version, "extraction_override": override}
    if company_id:
        body["company_id"] = company_id
    return await client.patch(f"/api/v1/inbox/documents/{document_id}", json=body)


@router.post(
    "/inbox/{document_id}/review", response_class=HTMLResponse, response_model=None
)
async def inbox_review_submit(
    request: Request, document_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    """action=save — persist reviewer edits (PATCH extraction_override).
    action=publish — persist, then publish as a DRAFT record.

    A 409 from either call re-renders the review page against the fresh
    document with a reload banner — the human re-applies their edits.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form_data = await request.form()
    form: dict[str, str] = {
        k: v for k, v in form_data.items() if isinstance(v, str)
    }
    action = (form.get("action") or "save").strip().lower()

    try:
        version = int(form.get("version", ""))
    except ValueError:
        request.session["flash"] = "Missing document version — please retry."
        return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)

    override = _override_from_form(form)
    company_id = (form.get("company_id") or "").strip() or None

    async with api_client(request) as client:
        patch_resp = await _patch_override(
            client, document_id, version, override, company_id
        )
        if patch_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if patch_resp.status_code == 409:
            fresh = await client.get(f"/api/v1/inbox/documents/{document_id}")
            if not fresh.is_success:
                request.session["flash"] = "Version conflict — refresh and try again."
                return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)
            return await _render_review(
                request, client, fresh.json(), conflict=True, status_code=409
            )
        if patch_resp.status_code in (404, 503):
            return _gate_page(request, patch_resp.status_code)
        if not patch_resp.is_success:
            request.session["flash"] = f"Save failed: HTTP {patch_resp.status_code}"
            return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)

        doc = patch_resp.json()

        if action != "publish":
            request.session["flash"] = "Review saved."
            return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)

        # ── Publish ────────────────────────────────────────────────────
        record_kind = (form.get("record_kind") or "EXPENSE").strip().upper()
        errors: dict[str, str] = {}
        if not company_id:
            errors["company_id"] = "Company is required to publish."
        if not (form.get("contact_id") or "").strip():
            errors["contact_id"] = "Contact is required to publish."
        if not (form.get("payment_account_id") or "").strip():
            errors["payment_account_id"] = "Payment account is required."
        if not (form.get("date") or "").strip():
            errors["date"] = "Date is required."
        lines = _parse_lines(form)
        if not lines:
            errors["lines"] = "At least one line is required."
        if errors:
            return await _render_review(
                request, client, doc, errors=errors, status_code=422,
                form_values=form,
            )

        publish_lines = []
        for line in lines:
            publish_lines.append(
                {
                    "description": line.get("description") or "(no description)",
                    "account_id": line.get("account_id"),
                    "tax_code_id": line.get("tax_code_id") or None,
                    "quantity": str(line.get("quantity") or "1"),
                    "unit_price": str(line.get("unit_price") or "0"),
                    **(
                        {"project_id": line["project_id"]}
                        if line.get("project_id")
                        else {}
                    ),
                }
            )

        payload: dict[str, Any] = {
            "record_kind": record_kind,
            "company_id": company_id,
            "contact_id": form["contact_id"].strip(),
            "date": form["date"].strip(),
            "payment_account_id": form["payment_account_id"].strip(),
            "lines": publish_lines,
        }
        reference = (form.get("invoice_number") or "").strip()
        if reference:
            payload["reference"] = reference
        notes = (form.get("notes") or "").strip()
        if notes:
            payload["notes"] = notes

        idempotency_key = form.get("idempotency_key") or str(uuid.uuid4())
        pub_resp = await client.post(
            f"/api/v1/inbox/documents/{document_id}/publish",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )

        if pub_resp.status_code == 401:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if pub_resp.status_code == 409:
            fresh = await client.get(f"/api/v1/inbox/documents/{document_id}")
            doc = fresh.json() if fresh.is_success else doc
            return await _render_review(
                request, client, doc, conflict=True, status_code=409
            )
        if pub_resp.status_code in (404, 503):
            # 503 here can also be the idempotency in-flight response —
            # surface it as a retry flash rather than the vault page.
            try:
                code = pub_resp.json().get("code")
            except Exception:
                code = None
            if code == "request_in_flight":
                request.session["flash"] = (
                    "Publish already in progress — retry in a second."
                )
                return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)
            return _gate_page(request, pub_resp.status_code)

        if pub_resp.status_code == 201:
            body = pub_resp.json()
            record = body.get("record", {})
            kind = (record.get("kind") or "EXPENSE").upper()
            record_id = record.get("id", "")
            request.session["flash"] = (
                f"Published as DRAFT {kind.replace('_', ' ').lower()} — "
                "source document attached."
            )
            detail_url = {
                "EXPENSE": f"/expenses/{record_id}",
                "BILL": f"/bills/{record_id}",
                "CREDIT_NOTE": f"/credit-notes/{record_id}",
            }.get(kind, "/inbox")
            return RedirectResponse(url=detail_url, status_code=303)

        # 422 (e.g. BILL/CREDIT_NOTE before phase 2) and anything else —
        # degrade gracefully: banner on the review page, nothing lost.
        detail = f"Publish failed: HTTP {pub_resp.status_code}"
        try:
            body_detail = pub_resp.json().get("detail")
            if isinstance(body_detail, str):
                detail = body_detail
            elif isinstance(body_detail, list) and body_detail:
                detail = body_detail[0].get("msg", detail)
            else:
                message = pub_resp.json().get("message")
                if message:
                    detail = message
        except Exception:
            pass
        fresh = await client.get(f"/api/v1/inbox/documents/{document_id}")
        doc = fresh.json() if fresh.is_success else doc
        return await _render_review(
            request, client, doc,
            errors={"__all__": detail},
            status_code=422 if pub_resp.status_code == 422 else 200,
            form_values=form,
        )


# ---------------------------------------------------------------------------
# Manual extraction retry
# ---------------------------------------------------------------------------


@router.post(
    "/inbox/{document_id}/extract", response_class=HTMLResponse, response_model=None
)
async def inbox_extract_retry(
    request: Request, document_id: uuid.UUID
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/inbox/documents/{document_id}/extract", timeout=60.0
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.status_code == 404:
        request.session["flash"] = (
            "AI extraction is not enabled on this edition — key the "
            "document manually."
        )
    elif resp.status_code == 409:
        request.session["flash"] = "Document is not in a re-extractable state."
    elif not resp.is_success:
        request.session["flash"] = f"Extraction retry failed: HTTP {resp.status_code}"
    else:
        request.session["flash"] = "Extraction re-run complete."
    return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@router.post(
    "/inbox/{document_id}/reject", response_class=HTMLResponse, response_model=None
)
async def inbox_reject(
    request: Request, document_id: uuid.UUID
) -> RedirectResponse:
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    reason = str(form.get("reason") or "").strip().upper()
    if reason not in _REJECT_REASONS:
        request.session["flash"] = "Pick a reject reason."
        return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)
    note = str(form.get("note") or "").strip() or None

    payload: dict[str, Any] = {"reason": reason}
    if note:
        payload["note"] = note

    async with api_client(request) as client:
        resp = await client.post(
            f"/api/v1/inbox/documents/{document_id}/reject", json=payload
        )

    if resp.status_code == 401:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if resp.is_success:
        request.session["flash"] = "Document rejected."
        return RedirectResponse(url="/inbox", status_code=303)
    if resp.status_code == 409:
        request.session["flash"] = "Document can no longer be rejected."
    else:
        request.session["flash"] = f"Reject failed: HTTP {resp.status_code}"
    return RedirectResponse(url=f"/inbox/{document_id}", status_code=303)


# ---------------------------------------------------------------------------
# Inline supplier quick-create (from vendor_name) — HTMX select swap
# ---------------------------------------------------------------------------


@router.post(
    "/inbox/{document_id}/quick-contact",
    response_class=HTMLResponse,
    response_model=None,
)
async def inbox_quick_contact(
    request: Request, document_id: uuid.UUID
) -> HTMLResponse | RedirectResponse:
    """Create a SUPPLIER contact named after the extracted vendor and
    return the refreshed contact ``<select>`` fragment with it selected."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    name = str(form.get("vendor_name") or "").strip()
    error: str | None = None
    selected_id = ""

    async with api_client(request) as client:
        if not name:
            error = "No vendor name to create a contact from."
        else:
            resp = await client.post(
                "/api/v1/contacts",
                json={"name": name, "contact_type": "SUPPLIER"},
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            )
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/login", status_code=303)
            if resp.status_code == 201:
                selected_id = resp.json().get("id", "")
            else:
                detail = f"Contact create failed (HTTP {resp.status_code})"
                try:
                    body_detail = resp.json().get("detail")
                    if isinstance(body_detail, str):
                        detail = body_detail
                    elif isinstance(body_detail, list) and body_detail:
                        detail = body_detail[0].get("msg", detail)
                except Exception:
                    pass
                error = detail

        contacts: list[dict] = []
        for ctype in ("SUPPLIER", "BOTH"):
            r = await client.get(
                "/api/v1/contacts", params={"type": ctype, "limit": 500, "offset": 0}
            )
            if r.is_success:
                contacts.extend(r.json().get("items", []))

    return _TEMPLATES.TemplateResponse(
        request,
        "inbox/_contact_select.html",
        {
            "contacts": contacts,
            "selected_contact_id": selected_id,
            "quick_create_error": error,
            "doc_id": str(document_id),
        },
        status_code=200 if not error else 422,
    )
