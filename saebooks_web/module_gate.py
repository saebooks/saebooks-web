"""Connection-level degrade layer — M2 app-lane step 7.

Distinguishes the two engine "module down" shapes from everything else so
routes render a degraded panel instead of a white-screen 500:

1. Connection-level failure — ``httpx.ConnectError`` / ``TimeoutException`` /
   ``RequestError``: raised as exceptions by the client, wrapped into
   :class:`ModuleUnavailable` by ``api_client.py``'s ``try/except`` around
   ``yield client``.
2. Engine module-unavailable 503 — a normal ``httpx.Response`` whose body
   matches one of the engine's two stub shapes (see
   :func:`_is_module_unavailable_503`), converted to
   :class:`ModuleUnavailable` by the response event hook before the caller
   ever sees the response.

Any other 503 (e.g. ato_sbr.py's "Encryption not configured on server"
business-logic 503) passes through untouched — existing call-site
status-code branches keep behaving exactly as they do today.

``main.py`` registers an ``@app.exception_handler(ModuleUnavailable)`` that
renders ``templates/_partials/degraded_panel.html`` at 503 for any route
that doesn't catch it locally.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi.responses import RedirectResponse
from starlette.requests import Request


class ModuleUnavailable(httpx.RequestError):
    """Raised when a downstream module is down: connection failure, timeout,
    or the engine's module-unavailable 503 stub. Caught by main.py's
    exception_handler and rendered as the shared degraded-panel partial.

    Subclasses ``httpx.RequestError`` DELIBERATELY: 16 modules (tax_returns,
    billing, public_auth, the SSO flows, …) have deliberate
    ``except httpx.RequestError`` branches with tailored UX (inline error /
    flash+redirect). Wrapping their transport errors in a non-RequestError
    type would silently bypass every one of them and replace their curated
    error surfaces with the generic degraded panel (caught by
    test_tax_returns_*_network_error). As a subclass, those sites keep
    working byte-for-byte; only routes with NO handler fall through to
    main.py's degraded-panel handler.
    """

    def __init__(self, module_id: str | None = None, detail: str = "") -> None:
        self.module_id = module_id
        self.detail = detail
        super().__init__(detail or f"module unavailable: {module_id}")


def _is_module_unavailable_503(resp: httpx.Response) -> str | None:
    """Return the module id if resp is one of the engine's TWO
    module-unavailable 503 shapes, else None. Never raises — a body that
    isn't JSON or doesn't match either shape is treated as an ordinary 503
    (existing call-site handling applies).

    There are two distinct engine sources of a module-unavailable 503, and
    the degrade layer must catch BOTH (verified against engine wave-1 +
    wave-2a):
      1. GUARDED-IMPORT STUB (wave 1) — a router that failed to import at
         boot. Body: ``{"status": "unavailable", "module": "<id>"}``.
      2. DELEGATED-SERVICE ERROR (wave 2a) — a live delegated app (capture /
         preaccounting / platform) is unreachable AND has no in-process
         fallback. Body is RFC 7807 problem+json:
         ``{"status": 503, "code": "module_unavailable", "module": "<id>", ...}``
         Here ``status`` is the NUMERIC 503 (RFC 7807's mandatory field) and
         the discriminator is ``code == "module_unavailable"`` — do NOT key
         on a string ``status`` for this one. (Delegation is OFF by default
         in M2, so shape 2 only appears once delegated apps are deployed
         live — but it is handled now so the degrade layer is correct when
         they are.)
    """
    if resp.status_code != 503:
        return None
    # Accept application/json AND RFC 7807's application/problem+json —
    # "application/json" is NOT a substring of "application/problem+json",
    # so match on the json suffix family, not the exact type.
    content_type = resp.headers.get("content-type", "")
    if "json" not in content_type:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    # Shape 1 — guarded-import stub.
    if body.get("status") == "unavailable" and body.get("module"):
        return str(body["module"])
    # Shape 2 — delegated-service RFC 7807 error.
    if body.get("code") == "module_unavailable":
        return str(body.get("module") or "unknown")
    return None


async def _module_unavailable_response_hook(resp: httpx.Response) -> None:
    """httpx response event_hook — raises ModuleUnavailable for EITHER engine
    module-unavailable 503 shape. Does not touch 401/404/other 503s (e.g.
    ato_sbr.py's legitimate business-logic 503 stays untouched).
    """
    await resp.aread()  # event hooks must read the body before it's consumed elsewhere
    module_id = _is_module_unavailable_503(resp)
    if module_id:
        raise ModuleUnavailable(
            module_id=module_id, detail="module unavailable (engine-reported)"
        )


@dataclass
class GateResult:
    ok: bool
    redirect: RedirectResponse | None = None  # 401 → caller returns this
    feature_disabled: bool = False  # 404 → caller renders upsell
    stubbed: bool = False  # 403 + {"stub": true} → caller renders stub badge


async def _module_gate(resp: httpx.Response, request: Request) -> GateResult:
    """Classify a response's status code into the existing four buckets.

    Connection failures and module-unavailable 503s never reach here —
    they're already ModuleUnavailable exceptions by the time a call site's
    try/except (or the absence of one) would see them. This only classifies
    the already-existing status-code branches so new route modules don't
    hand-roll the same if/elif ato_sbr.py has five times over.
    """
    if resp.status_code == 401:
        request.session.clear()
        return GateResult(
            ok=False, redirect=RedirectResponse(url="/login", status_code=303)
        )
    if resp.status_code == 404:
        return GateResult(ok=False, feature_disabled=True)
    if resp.status_code == 403:
        try:
            stub = resp.json().get("stub") is True
        except ValueError:
            stub = False
        if stub:
            return GateResult(ok=False, stubbed=True)
    return GateResult(ok=resp.is_success)
