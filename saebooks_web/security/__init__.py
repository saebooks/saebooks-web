"""Security primitives: CSRF protection (Layers 2 + 3) and helpers.

Layer 1 (samesite=strict) lives in main.py because it is a direct argument
to Starlette's SessionMiddleware.

Layer 2 — origin_referer_middleware:
    Reject state-changing requests whose Origin/Referer does not match the
    configured site origin.  Implemented as a pure ASGI middleware so it can
    short-circuit before any route handler runs and without consuming the
    request body.

Layer 3 — CSRFMiddleware + csrf_input + verify_csrf_form:
    Per-session CSRF token embedded in every form via the {{ csrf_input() }}
    Jinja macro.  An ASGI middleware reads the form body, verifies the token
    matches request.session['csrf_token'], rejects 403 on mismatch, and
    re-injects the body so the downstream handler can read it normally.

Side-effect on import
---------------------
Importing this module patches ``fastapi.templating.Jinja2Templates`` so that
every templates-instance created afterwards has ``csrf_input`` registered as
a Jinja global.  This avoids touching the 30+ route modules that each
construct their own ``Jinja2Templates``.

To work on an already-instantiated templates env (route modules imported
before security), call ``ensure_csrf_global(templates)`` directly.

This patch is idempotent — re-importing the module does not re-wrap the
init; subsequent instantiations still get the global registered exactly
once.
"""
from __future__ import annotations

import logging

from saebooks_web.security.csrf import (
    CSRFMiddleware,
    OriginRefererMiddleware,
    csrf_input,
    ensure_csrf_token,
    verify_csrf_form,
)

_logger = logging.getLogger(__name__)


def ensure_csrf_global(templates) -> None:
    """Register ``csrf_input`` as a Jinja global on the given templates env.

    Called by the patched ``Jinja2Templates.__init__`` for any new env, and
    also exposed for callers that want to retrofit an env that was created
    before the security module was imported.
    """
    try:
        templates.env.globals.setdefault("csrf_input", csrf_input)
    except AttributeError:  # pragma: no cover — defensive
        _logger.warning(
            "ensure_csrf_global: templates object %r has no .env.globals", templates
        )


def _patch_jinja_templates() -> None:
    """Monkey-patch ``Jinja2Templates.__init__`` to register ``csrf_input``.

    Idempotent — checks for a sentinel attribute before re-wrapping.
    """
    from fastapi.templating import Jinja2Templates

    if getattr(Jinja2Templates.__init__, "_saebooks_csrf_patched", False):
        return  # already patched

    _orig_init = Jinja2Templates.__init__

    def _patched_init(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        _orig_init(self, *args, **kwargs)
        ensure_csrf_global(self)
        # Also register the feature-flag Jinja global so templates can do
        # {% if is_feature_enabled('hard_delete') %} — see features.py.
        try:
            from saebooks_web.features import register_feature_global
            register_feature_global(self)
        except Exception:
            pass
        # Also register the brand-config Jinja global (current_brand()) —
        # see brand.py. Same injection hook, deployment-level SAEBOOKS_BRAND.
        try:
            from saebooks_web.brand import register_brand_global
            register_brand_global(self)
        except Exception:
            pass
        # Also register the gettext callables (_ / gettext / ngettext) —
        # see i18n/__init__.py. Same injection hook; call-time-resolving
        # against a request-scoped contextvar, NEVER
        # install_gettext_translations on this shared env (see that
        # module's docstring for the concurrency landmine it avoids).
        try:
            from saebooks_web.i18n import register_i18n_global
            register_i18n_global(self)
        except Exception:
            pass

    _patched_init._saebooks_csrf_patched = True  # type: ignore[attr-defined]
    Jinja2Templates.__init__ = _patched_init  # type: ignore[method-assign]


# Apply on import.  Order matters in main.py: this module must be imported
# BEFORE any route module that constructs a Jinja2Templates() (otherwise
# existing instances don't get the patched __init__ and need manual
# retrofitting via ensure_csrf_global).
_patch_jinja_templates()


__all__ = [
    "CSRFMiddleware",
    "OriginRefererMiddleware",
    "csrf_input",
    "ensure_csrf_global",
    "ensure_csrf_token",
    "verify_csrf_form",
]
