"""Demo auto-login — bootstrap a session for the public Cashbook demo.

When ``SAEBOOKS_DEMO_AUTOLOGIN_EMAIL`` and ``SAEBOOKS_DEMO_AUTOLOGIN_PASSWORD``
are set, every unauthenticated request to a non-skip path performs a
server-side login against ``/api/v1/auth/login`` using those credentials and
stuffs the resulting token + profile into the signed session cookie. The
end-user never sees the login screen.

This is intentionally NOT gated by network position — it is opt-in via env
var, only enabled on the cashbook-demo container. Any host that flips this
on becomes a public demo where the configured account is automatically
shared. NEVER set these env vars on the main app.saebooks.com.au stack.

Visitors land on / and (in cashbook-mode demos) get redirected to /cashbook
so the demo lands on the relevant surface immediately.

Binding-key (Reading A, 2026-06-24)
-------------------------------------
In ephemeral mode the session carries a per-visitor **binding key**: the
visitor's ``api_token`` that maps 1:1 to their provisioned demo company.
On a root (``/``) visit the middleware checks whether a live binding key is
already present (via ``/auth/me``). If it is, the visitor's existing company
is REUSED — no re-provision, no data loss.  Only keyless visits (or visits
where the key's company was reaped) mint a fresh tenant.

This fixes the "Home button resets the demo" bug: navigating to ``/`` no
longer wipes the visitor's company when they already have a live key.

Cloudflare Turnstile gate (SAEBOOKS_DEMO_TURNSTILE_ENABLED)
--------------------------------------------------------------
When ``SAEBOOKS_DEMO_TURNSTILE_ENABLED=1``, ``TURNSTILE_SITE_KEY``, and
``TURNSTILE_SECRET_KEY`` are all set, the MINT path is gated by a Turnstile
challenge:

- A keyless GET to ``/`` renders ``demo/turnstile_gate.html`` (the Turnstile
  landing page) instead of immediately provisioning.
- The visitor completes the challenge; the widget POSTs the response token to
  ``/demo/turnstile-provision`` (an internal path handled entirely within this
  middleware — no FastAPI route needed).
- The middleware verifies the token server-side (``POST siteverify``) and, on
  success, provisions a new tenant and redirects to the land path.
- Returning visitors with a live binding key NEVER see the widget — they are
  reused immediately on root visits.

Keys missing or flag off → Turnstile is a no-op; behaviour falls back to
direct provision on keyless visits (safe for dev / staging).
"""
from __future__ import annotations

import html as _html
import logging
import os
import urllib.parse
from datetime import datetime, timezone

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from saebooks_web.brand import current_brand
from saebooks_web.config import settings

_log = logging.getLogger("saebooks_web.demo_autologin")

# Anything matching these prefixes bypasses autologin entirely (static
# assets, health, the login routes themselves so manual login still
# works as an escape hatch, logout, demo-marker pages).
_SKIP_PREFIXES = (
    "/static/",
    "/healthz",
    "/favicon.ico",
    "/login",
    "/logout",
    "/oauth/",
    # Server-to-server (engine → /internal/render); must not trigger a demo
    # auto-login / provision round-trip.
    "/internal/",
)

# Internal path handled by this middleware — never forwarded to FastAPI.
_TURNSTILE_PROVISION_PATH = "/demo/turnstile-provision"


def _email() -> str:
    return os.environ.get("SAEBOOKS_DEMO_AUTOLOGIN_EMAIL", "").strip()


def _password() -> str:
    return os.environ.get("SAEBOOKS_DEMO_AUTOLOGIN_PASSWORD", "")


def _land_path() -> str:
    """Where to redirect after autologin when path is /.

    Defaults to /cashbook for the cashbook demo. Override via
    SAEBOOKS_DEMO_LAND_PATH for other demo flavours later.
    """
    return os.environ.get("SAEBOOKS_DEMO_LAND_PATH", "/cashbook").strip() or "/"


def _enabled() -> bool:
    return bool(_email()) and bool(_password())


def _ephemeral_enabled() -> bool:
    """Ephemeral per-visit demo mode: provision a fresh, isolated, seeded tenant
    on each root visit via the engine's ``POST /internal/demo/provision`` instead
    of logging into one shared account. Independent of the fixed-cred autologin
    above; when on it takes precedence. Toggled by ``SAEBOOKS_DEMO_EPHEMERAL``."""
    return os.environ.get("SAEBOOKS_DEMO_EPHEMERAL", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _internal_secret() -> str:
    return os.environ.get("DEMO_INTERNAL_SECRET", "").strip()


def _turnstile_enabled() -> bool:
    """True when Turnstile is fully configured and switched on.

    Requires ALL of:
    - ``SAEBOOKS_DEMO_TURNSTILE_ENABLED`` is truthy
    - ``TURNSTILE_SITE_KEY`` is set
    - ``TURNSTILE_SECRET_KEY`` is set

    Any missing piece → Turnstile is silently disabled (safe fallback).
    """
    flag = os.environ.get("SAEBOOKS_DEMO_TURNSTILE_ENABLED", "").strip().lower()
    if flag in ("", "0", "false", "no", "off"):
        return False
    return bool(os.environ.get("TURNSTILE_SITE_KEY", "").strip()) and bool(
        os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
    )


def _turnstile_site_key() -> str:
    return os.environ.get("TURNSTILE_SITE_KEY", "").strip()


def _turnstile_secret_key() -> str:
    return os.environ.get("TURNSTILE_SECRET_KEY", "").strip()


def _source_ip(request: Request) -> str | None:
    """Best-effort original client IP, forwarded so the engine's per-IP provision
    rate-limit sees the visitor (not the web container)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return request.client.host if request.client is not None else None


# ---------------------------------------------------------------------------
# Turnstile landing page (inline HTML — no Jinja2 template dependency
# needed here; keep the gate self-contained within the middleware module).
# ---------------------------------------------------------------------------

_TURNSTILE_GATE_HTML = """\
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{brand_name} — Demo</title>
  <script>
    (function () {{
      var stored = localStorage.getItem('saebooks-theme');
      var dark = stored === 'dark' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches);
      if (dark) {{
        document.documentElement.classList.add('dark');
        document.documentElement.setAttribute('data-theme', 'dark');
      }}
    }})();
  </script>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #f9fafb; --fg: #111827; --card: #ffffff; --border: #e5e7eb;
      --primary: #194291; --primary-fg: #ffffff;
    }}
    html.dark {{
      --bg: #0e1a2a; --fg: #e2e8f0; --card: #1a2a3a; --border: #2d3748;
    }}
    body {{
      background: var(--bg); color: var(--fg);
      font-family: Inter, system-ui, -apple-system, sans-serif;
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      padding: 1.5rem;
    }}
    .card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 0.75rem; padding: 2.5rem 2rem;
      max-width: 420px; width: 100%; text-align: center;
      box-shadow: 0 4px 24px rgba(0,0,0,.08);
    }}
    .logo {{
      font-size: 1.5rem; font-weight: 700; color: var(--primary); margin-bottom: .25rem;
    }}
    .tagline {{ font-size: .875rem; opacity: .65; margin-bottom: 1.75rem; }}
    .features {{ list-style: none; text-align: left; margin-bottom: 1.75rem; font-size: .9rem; }}
    .features li {{ padding: .3rem 0; }}
    .features li::before {{ content: "✓ "; color: var(--primary); font-weight: 700; }}
    .cf-wrap {{ display: flex; justify-content: center; margin-bottom: 1rem; }}
    .notice {{ font-size: .75rem; opacity: .5; margin-top: 1rem; }}
    .error-msg {{
      background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5;
      border-radius: .5rem; padding: .75rem 1rem; margin-bottom: 1rem;
      font-size: .875rem; display: {error_display};
    }}
    html.dark .error-msg {{ background: #450a0a; color: #fca5a5; border-color: #991b1b; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">{brand_name}</div>
    <div class="tagline">{tagline}</div>
    <ul class="features">{features_html}</ul>
    <div class="error-msg">{error_msg}</div>
    <form method="POST" action="{provision_path}">
      <div class="cf-wrap">
        <div class="cf-turnstile" data-sitekey="{site_key}" data-theme="auto" data-callback="onTurnstileSuccess"></div>
      </div>
      <noscript><p style="color:#991b1b;margin-bottom:1rem">JavaScript must be enabled to use this demo.</p></noscript>
    </form>
    <div class="notice">Your demo is private, isolated and auto-deleted after 2 hours.</div>
  </div>
  <script>
    // Auto-submit the form once Turnstile resolves. The widget div wires
    // data-callback="onTurnstileSuccess"; Turnstile invokes it with the token
    // on completion (invisible/managed run automatically, the checkbox on click).
    function onTurnstileSuccess(token) {{
      document.querySelector('form').submit();
    }}
  </script>
</body>
</html>
"""


class DemoAutoLoginMiddleware(BaseHTTPMiddleware):
    """If the demo creds env vars are set, mint a session on the fly.

    Two modes: the original fixed-cred shared autologin (``_enabled``) and the
    ephemeral per-visit tenant provisioner (``_ephemeral_enabled``, preferred)."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if _ephemeral_enabled():
            return await self._dispatch_ephemeral(request, call_next)
        if not _enabled():
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Already authenticated — verify the token still works before trusting.
        # Stale cookies (e.g. from a wiped demo DB) carry a token for a user
        # that no longer exists; trusting them produces a redirect loop because
        # every downstream API call 401s. Verify via /auth/me, drop on failure.
        existing_token = request.session.get("api_token")
        if existing_token:
            try:
                async with httpx.AsyncClient(
                    base_url=settings.api_url, timeout=8.0
                ) as client:
                    me_resp = await client.get(
                        "/api/v1/auth/me",
                        headers={"Authorization": f"Bearer {existing_token}"},
                    )
            except httpx.RequestError as exc:
                _log.warning("demo autologin verify error: %r", exc)
                return await call_next(request)
            if me_resp.is_success:
                if path == "/":
                    land = _land_path()
                    if land != "/":
                        return RedirectResponse(land, status_code=303)
                return await call_next(request)
            _log.info(
                "demo autologin: dropping stale token (auth/me %s)",
                me_resp.status_code,
            )
            request.session.pop("api_token", None)
            request.session.pop("username", None)
            request.session.pop("user_role", None)

        # Server-side login.
        email = _email()
        password = _password()
        try:
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=8.0
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/login",
                    json={"email": email, "password": password},
                )
                if not resp.is_success:
                    _log.warning(
                        "demo autologin failed (%s): %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return await call_next(request)
                token = resp.json()["access_token"]
                me_resp = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError as exc:
            _log.warning("demo autologin transport error: %r", exc)
            return await call_next(request)

        request.session.pop("csrf_token", None)
        request.session.pop("active_company_id", None)
        request.session["api_token"] = token

        if me_resp.is_success:
            profile = me_resp.json()
            request.session["username"] = (
                profile.get("name")
                or profile.get("username")
                or profile.get("email")
                or ""
            )
            request.session["user_role"] = profile.get("role", "")

        # Send / → /cashbook (or whatever SAEBOOKS_DEMO_LAND_PATH says).
        if path == "/":
            land = _land_path()
            if land != "/":
                return RedirectResponse(land, status_code=303)

        return await call_next(request)

    # ------------------------------------------------------------------ #
    # Ephemeral per-visit mode                                            #
    # ------------------------------------------------------------------ #

    async def _token_valid(self, token: str) -> bool:
        """True if the demo JWT still authenticates (its tenant wasn't reaped)."""
        try:
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=8.0
            ) as client:
                r = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return r.is_success
        except httpx.RequestError:
            # Transport hiccup — don't churn a fresh tenant; treat as still valid.
            return True

    async def _provision(self, request: Request) -> dict | None:
        """Mint a fresh ephemeral demo tenant via the engine. Returns the
        provision payload (with a best-effort ``_profile``) or None on
        capacity / rate-limit / guard / transport failure — the caller then
        serves the request unprovisioned rather than 500ing."""
        headers: dict[str, str] = {}
        secret = _internal_secret()
        if secret:
            headers["X-Internal-Secret"] = secret
        ip = _source_ip(request)
        if ip:
            headers["X-Forwarded-For"] = ip
        try:
            # Generous timeout: a fresh tenant + dataset seed (esp. the cashbook
            # flavour, which posts journal entries) can take several seconds.
            async with httpx.AsyncClient(
                base_url=settings.api_url, timeout=25.0
            ) as client:
                resp = await client.post(
                    "/internal/demo/provision", headers=headers, json={}
                )
                if resp.status_code != 201:
                    _log.warning(
                        "demo provision non-201 (%s): %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None
                data = resp.json()
                if not data.get("access_token"):
                    return None
                profile: dict = {}
                try:
                    me = await client.get(
                        "/api/v1/auth/me",
                        headers={
                            "Authorization": f"Bearer {data['access_token']}"
                        },
                    )
                    if me.is_success:
                        profile = me.json()
                except httpx.RequestError:
                    pass
                data["_profile"] = profile
                return data
        except httpx.RequestError as exc:
            _log.warning("demo provision transport error: %r", exc)
            return None

    def _apply_provision_to_session(self, request: Request, data: dict) -> None:
        """Write provisioned tenant credentials into the session cookie.

        Also records the provisioned company/tenant identity + a UTC
        timestamp so the passive demo badge (see ``_partials/demo_banner``)
        and the live isolation-proof card (``routes/demo_isolation``) can
        render the visitor's own tenant identity without a further API call.
        Presence of ``demo_tenant_id`` in the session is the single marker
        that a request belongs to a provisioned ephemeral demo (both the
        banner and the isolation route gate on it)."""
        request.session.pop("csrf_token", None)
        request.session.pop("active_company_id", None)
        request.session["api_token"] = data["access_token"]
        profile = data.get("_profile") or {}
        request.session["username"] = (
            profile.get("name")
            or profile.get("username")
            or profile.get("email")
            or data.get("demo_user_email", "")
        )
        request.session["user_role"] = profile.get("role", "")
        # Demo-identity markers for the isolation surface. company_id/tenant_id
        # come straight off the provision payload (a strict superset of
        # /auth/login — see the engine's POST /internal/demo/provision); the
        # profile is a best-effort fallback for company_id only.
        company_id = data.get("company_id") or profile.get("company_id") or ""
        tenant_id = data.get("tenant_id") or profile.get("tenant_id") or ""
        request.session["demo_company_id"] = str(company_id)
        request.session["demo_tenant_id"] = str(tenant_id)
        request.session["demo_provisioned_at"] = (
            datetime.now(timezone.utc).strftime("%H:%M")
        )

    async def _verify_turnstile(self, token: str, ip: str | None) -> bool:
        """Verify a Turnstile response token via the Cloudflare siteverify API.

        Returns True on success, False on failure or network error.
        Always returns True when Turnstile is not enabled (safe default for
        the no-keys / flag-off case).
        """
        if not _turnstile_enabled():
            return True
        payload: dict = {
            "secret": _turnstile_secret_key(),
            "response": token,
        }
        if ip:
            payload["remoteip"] = ip
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data=payload,
                )
            result = r.json()
            success = result.get("success", False)
            if not success:
                _log.warning(
                    "turnstile verify failed: %s", result.get("error-codes", [])
                )
            return bool(success)
        except httpx.RequestError as exc:
            _log.warning("turnstile verify transport error: %r", exc)
            return False

    def _turnstile_gate_response(self, error_msg: str = "") -> HTMLResponse:
        """Return the Turnstile landing page HTML response.

        Brand name, tagline and feature bullets are pulled from the active
        brand (``SAEBOOKS_BRAND``) so a Tasur (EE) deployment renders Estonian
        copy with no code edit — see ``saebooks_web/brand.py``.
        """
        brand = current_brand()
        features_html = "".join(
            f"<li>{_html.escape(feat)}</li>" for feat in brand.demo_features
        )
        rendered = _TURNSTILE_GATE_HTML.format(
            brand_name=_html.escape(brand.name),
            tagline=_html.escape(brand.demo_tagline),
            features_html=features_html,
            site_key=_turnstile_site_key(),
            provision_path=_TURNSTILE_PROVISION_PATH,
            error_msg=error_msg,
            error_display="block" if error_msg else "none",
        )
        return HTMLResponse(content=rendered, status_code=200)

    async def _dispatch_ephemeral(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # ------------------------------------------------------------------ #
        # Turnstile POST handler — internal path; never reaches FastAPI.      #
        # ------------------------------------------------------------------ #
        if path == _TURNSTILE_PROVISION_PATH and request.method == "POST":
            return await self._handle_turnstile_provision(request)

        existing = request.session.get("api_token")

        # ------------------------------------------------------------------ #
        # Binding-key check: reuse the visitor's existing company if live.    #
        #                                                                      #
        # Old behaviour: ``need_fresh = path == "/" or not existing``          #
        #   → re-provisions on EVERY root visit, wiping the visitor's demo.   #
        #                                                                      #
        # New behaviour: treat a root visit like any other path when a        #
        # valid binding key is already present — reuse the existing company.  #
        # Only mint fresh when there is no key, or the key's company is gone. #
        # ------------------------------------------------------------------ #
        need_fresh = not existing
        if existing and not need_fresh:
            if not await self._token_valid(existing):
                need_fresh = True
                _log.info("demo: token stale (tenant reaped) — reprovisioning")

        if not need_fresh:
            # Valid binding key present — reuse the company.
            if path == "/":
                land = _land_path()
                if land != "/":
                    return RedirectResponse(land, status_code=303)
            return await call_next(request)

        # ------------------------------------------------------------------ #
        # No valid binding key — need to mint a fresh tenant.                 #
        # Gate the mint behind Turnstile if enabled.                          #
        # ------------------------------------------------------------------ #
        if _turnstile_enabled():
            # Show the Turnstile challenge; provisioning happens on POST.
            return self._turnstile_gate_response()

        # Turnstile off — provision directly (original behaviour for new visitors).
        data = await self._provision(request)
        if data is None:
            # Capacity / rate-limit / transient — let the request through
            # unprovisioned (renders the public landing, no 500).
            return await call_next(request)

        self._apply_provision_to_session(request, data)

        if path == "/":
            land = _land_path()
            if land != "/":
                return RedirectResponse(land, status_code=303)
        return await call_next(request)

    async def _handle_turnstile_provision(self, request: Request) -> HTMLResponse | RedirectResponse:
        """Handle POST /demo/turnstile-provision: verify token, provision, redirect.

        This path is only reached when SAEBOOKS_DEMO_TURNSTILE_ENABLED is on
        and a visitor has submitted the Turnstile challenge form.
        """
        ip = _source_ip(request)
        try:
            body = await request.body()
            form_data = dict(urllib.parse.parse_qsl(body.decode("utf-8", errors="replace")))
        except Exception:
            form_data = {}

        cf_token = form_data.get("cf-turnstile-response", "").strip()
        if not cf_token:
            _log.warning("turnstile provision: no cf-turnstile-response in POST body")
            return self._turnstile_gate_response(
                error_msg="Challenge not completed — please try again."
            )

        verified = await self._verify_turnstile(cf_token, ip)
        if not verified:
            _log.warning("turnstile provision: token verification failed")
            return self._turnstile_gate_response(
                error_msg="Verification failed — please try again."
            )

        data = await self._provision(request)
        if data is None:
            return self._turnstile_gate_response(
                error_msg="Demo capacity reached — please try again in a moment."
            )

        self._apply_provision_to_session(request, data)

        land = _land_path()
        return RedirectResponse(land, status_code=303)
