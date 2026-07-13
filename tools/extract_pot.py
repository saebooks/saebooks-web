#!/usr/bin/env python3
"""Extract the .pot via the babel API instead of the pybabel CLI.

Why not ``pybabel extract``: babel's default directory filter silently
skips directories whose basename starts with ``.`` or ``_`` — which in
this repo means every string in ``templates/_partials/`` and
``templates/_components/`` never entered the catalog and rendered as
English in every locale (found 2026-07-13 via the demo banner). The CLI
exposes no override; the API does. This script mirrors the Makefile's
former ``pybabel extract -F babel.cfg -o i18n/messages.pot`` invocation
with a directory filter that only skips hidden (dot) directories.
"""
from __future__ import annotations

import os
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import write_po

REPO_ROOT = Path(__file__).resolve().parent.parent
POT_PATH = REPO_ROOT / "i18n" / "messages.pot"

METHOD_MAP = [
    ("saebooks_web/**.py", "python"),
    ("templates/**.html", "jinja2"),
]
OPTIONS_MAP = {
    "templates/**.html": {"encoding": "utf-8"},
}


def keep_underscores(dirpath: str | os.PathLike) -> bool:
    """Skip hidden directories only — underscore dirs are template code."""
    return not os.path.basename(dirpath).startswith(".")


def main() -> None:
    catalog = Catalog(project="saebooks-web", charset="utf-8")
    for filename, lineno, message, comments, context in extract_from_dir(
        str(REPO_ROOT),
        method_map=METHOD_MAP,
        options_map=OPTIONS_MAP,
        directory_filter=keep_underscores,
    ):
        catalog.add(
            message,
            None,
            [(filename, lineno)],
            auto_comments=comments,
            context=context,
        )
    POT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(POT_PATH, "wb") as fh:
        write_po(fh, catalog, width=None)
    print(f"wrote {POT_PATH} ({len([m for m in catalog if m.id])} msgids)")


if __name__ == "__main__":
    main()
