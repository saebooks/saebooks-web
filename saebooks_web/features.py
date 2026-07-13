"""Feature-flag check for the web layer.

The web layer is a thin client of the saebooks API and doesn't import any
saebooks internals. For UI rendering decisions (e.g. "should this kebab
include a hard-delete option?") the web layer needs to know which feature
flags are active under the current ``SAEBOOKS_EDITION`` env value.

Rather than duplicate the full tier→flag map from saebooks/services/features.py,
this module hardcodes a minimal subset: the dev-only flags that the web UI
cares about. If the published tiers add UI-affecting flags later, this map
will need to grow.

Read by templates via the ``is_feature_enabled(flag)`` Jinja global, which
is registered onto every Jinja2Templates env by ``register_feature_global``.
"""
from __future__ import annotations

import os

# Map edition → set of flags whose UI should render. Mirrors the relevant
# slice of saebooks/services/features.py — only flags the web template
# layer actually keys off. Keep this minimal; one-way trust between web
# and api on flag enablement.
_EDITION_FLAGS: dict[str, frozenset[str]] = {
    "community":  frozenset(),
    "offline":    frozenset(),
    # eid_auth — Estonian eID login (Smart-ID / Mobiil-ID). Paid tiers
    # only: SK ID Solutions bills production authentications per
    # transaction, so the free/community tier never renders or routes it.
    # Engine-side parity flag (FLAG_EID_AUTH) is engine-lane work.
    "business":   frozenset({"eid_auth"}),
    "pro":        frozenset({"eid_auth"}),
    "enterprise": frozenset({"eid_auth"}),
    "developer":  frozenset({"hard_delete", "dev_tools", "eid_auth"}),
}


def _current_edition() -> str:
    return os.environ.get("SAEBOOKS_EDITION", "community").strip().lower() or "community"


def is_feature_enabled(flag: str) -> bool:
    """True when ``flag`` is active under the current SAEBOOKS_EDITION env."""
    edition = _current_edition()
    return flag in _EDITION_FLAGS.get(edition, frozenset())


def current_edition() -> str:
    """Return the active edition string (e.g. ``"developer"``, ``"pro"``).

    Templates use this to render the edition badge in the side-nav, swap
    in a distinct colour on dev instances, etc.
    """
    return _current_edition()


def is_dev_edition() -> bool:
    """True when the active edition is ``developer``.

    Convenience for templates that just want to know "should I render the
    dev-specific UI affordance?" without naming a specific flag.
    """
    return _current_edition() == "developer"


def register_feature_global(templates) -> None:
    """Add feature-flag helpers to a Jinja2Templates env's globals.

    Called from the patched Jinja2Templates.__init__ so every templates
    instance gets the globals automatically. Idempotent — ``setdefault``
    no-ops if already present.
    """
    try:
        templates.env.globals.setdefault("is_feature_enabled", is_feature_enabled)
        templates.env.globals.setdefault("current_edition", current_edition)
        templates.env.globals.setdefault("is_dev_edition", is_dev_edition)
    except AttributeError:
        pass
