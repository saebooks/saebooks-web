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
    "business":   frozenset(),
    "pro":        frozenset(),
    "enterprise": frozenset(),
    "developer":  frozenset({"hard_delete", "dev_tools"}),
}


def _current_edition() -> str:
    return os.environ.get("SAEBOOKS_EDITION", "community").strip().lower() or "community"


def is_feature_enabled(flag: str) -> bool:
    """True when ``flag`` is active under the current SAEBOOKS_EDITION env."""
    edition = _current_edition()
    return flag in _EDITION_FLAGS.get(edition, frozenset())


def register_feature_global(templates) -> None:
    """Add ``is_feature_enabled`` to a Jinja2Templates env's globals.

    Called from the patched Jinja2Templates.__init__ so every templates
    instance gets the global automatically. Idempotent — ``setdefault``
    no-ops if already present.
    """
    try:
        templates.env.globals.setdefault("is_feature_enabled", is_feature_enabled)
    except AttributeError:
        pass
