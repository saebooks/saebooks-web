"""Version exposure for the web layer.

Until 2026-08-11 the sidebar rendered a hardcoded ``v2026.05`` literal
(``base.html``), frozen since May and therefore wrong on every deployment that
displayed it — Richard's books and both demos. ``main.py`` separately hardcoded
``version="0.1.3"`` while ``pyproject.toml`` said ``0.3``, so the component
disagreed with itself as well.

Both now read ``saebooks_web.__version__``, which ``scripts/bump-version.sh``
keeps in lockstep with ``pyproject.toml``. Per Richard's 2026-08-11 decision the
scheme is *per-component, auto-derived*: each component keeps its own number and
reads it from its own manifest, rather than every surface carrying a hand-typed
copy that someone has to remember to bump.

``app_version()`` is registered as a Jinja global on the same
``Jinja2Templates.__init__`` patch hook that already delivers ``current_brand``
and ``current_edition`` (see ``saebooks_web/security/__init__.py``) — one hook
for all template envs, no route module touched.
"""
from __future__ import annotations

from saebooks_web import __version__


def app_version() -> str:
    """Return this component's version string, without a leading ``v``."""
    return __version__


def register_version_global(templates) -> None:
    """Add the ``app_version`` Jinja global to a Jinja2Templates env.

    Mirrors ``register_brand_global`` / ``register_feature_global`` exactly.
    Registered as the callable rather than a pre-resolved string purely for
    consistency with its siblings on the same hook.
    """
    try:
        templates.env.globals.setdefault("app_version", app_version)
    except AttributeError:
        pass
