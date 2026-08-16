"""SAE Books web frontend package.

``__version__`` is the canonical version literal for this component, mirroring
the convention the engine already uses in ``saebooks/__init__.py``. Everything
that displays a version reads it from here:

  * OpenAPI ``/api/docs`` info.version   (``main.py``: ``version=__version__``)
  * the sidebar version badge            (``base.html``: ``{{ app_version() }}``)

``pyproject.toml``'s ``[project] version`` must equal it — that is what gets
baked into installed package metadata. ``scripts/bump-version.sh`` sets both in
one command and ``--check`` guards the drift; ``tests/test_version_display.py``
asserts the runtime surfaces agree.

Why a literal and not a dynamic ``importlib.metadata`` read: the Dockerfile
installs dependencies before copying the source tree, so setuptools attr
resolution would ``ModuleNotFound`` at build time — the same reason the engine
keeps a literal.
"""

__version__ = "0.6.1"
