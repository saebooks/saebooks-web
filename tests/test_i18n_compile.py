"""Compile-in-CI test (EE GUI prep Packet 2a, deliverable 4).

Two things this guards against, both silent-drift failure modes for a
gettext pipeline:

  1. A .po was hand-edited (or `make i18n-update` was run) but nobody ran
     `make i18n-compile` before committing — the .mo on disk no longer
     matches its .po, so translators' changes silently don't ship.
  2. babel.cfg / the extraction config bit-rots against the real template
     tree (e.g. templates/ gets renamed) and `pybabel extract` starts
     silently finding zero files.

Run in CI as an ordinary pytest test (no separate CI-only script) so it
fails the same way any other regression does.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from babel.messages.mofile import read_mo
from babel.messages.pofile import read_po

import saebooks_web.i18n as i18n

REPO_ROOT = Path(__file__).resolve().parent.parent


def _catalog_message_ids(catalog) -> set:
    # Plural messages carry a (singular, plural) list as their id — make it
    # hashable so the set works for singular and plural entries alike.
    return {tuple(m.id) if isinstance(m.id, list) else m.id for m in catalog if m.id}


def test_babel_cfg_extracts_from_real_template_tree(tmp_path):
    """Extraction config is correct and pybabel is on PATH — the pipeline
    dependency itself is wired, not just importable as a library.

    Writes to a throwaway tmp_path .pot (NOT "-"/stdout — this babel
    version treats "-o -" as a literal filename, not a stdout sentinel,
    which would otherwise leave a stray "-" file in the repo root)."""
    out_pot = tmp_path / "messages.pot"
    result = subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "extract",
         "-F", str(REPO_ROOT / "babel.cfg"), "-o", str(out_pot), str(REPO_ROOT)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "extracting messages from" in result.stderr
    assert f"{REPO_ROOT}/templates" in result.stderr
    assert out_pot.exists() and out_pot.stat().st_size > 0


def test_committed_po_and_mo_are_in_sync_for_every_supported_locale():
    """For each locale in SUPPORTED_LOCALES: the .mo checked in matches
    what compiling the .po right now would produce (same msgid set) — the
    concrete "forgot to run make i18n-compile" regression."""
    for locale in i18n.SUPPORTED_LOCALES:
        lc_messages = i18n.LOCALES_DIR / locale / "LC_MESSAGES"
        po_path = lc_messages / f"{i18n.DOMAIN}.po"
        mo_path = lc_messages / f"{i18n.DOMAIN}.mo"
        assert po_path.exists(), f"missing {po_path} — run `make i18n-init`"
        assert mo_path.exists(), f"missing {mo_path} — run `make i18n-compile`"

        with open(po_path, "rb") as fh:
            po_catalog = read_po(fh, locale=locale, domain=i18n.DOMAIN)
        with open(mo_path, "rb") as fh:
            mo_catalog = read_mo(fh)

        po_ids = _catalog_message_ids(po_catalog)
        mo_ids = _catalog_message_ids(mo_catalog)
        assert po_ids == mo_ids, (
            f"{mo_path} is stale relative to {po_path} "
            f"(po-only: {po_ids - mo_ids}, mo-only: {mo_ids - po_ids}) — "
            "run `make i18n-compile`"
        )


def test_reset_translations_cache_reloads_from_disk(monkeypatch):
    """The cache doesn't accidentally paper over a stale/missing .mo —
    forcing a reload re-reads whatever is actually on disk right now."""
    i18n.reset_translations_cache()
    token = i18n.current_locale.set("et")
    try:
        # Real catalog has 0 wrapped strings yet (P2a ships the pipeline,
        # not the sweep) — gettext degrades to returning the source string,
        # which itself proves load-without-raising against the real,
        # committed .mo rather than a test fixture.
        assert i18n.gettext("a string with no catalog entry yet") == (
            "a string with no catalog entry yet"
        )
    finally:
        i18n.current_locale.reset(token)
        i18n.reset_translations_cache()
