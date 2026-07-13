#!/usr/bin/env python3
"""Fill untranslated et/ru .po msgids via TartuNLP, adapted from
tasur-site/tools/translate.py (EE GUI prep, Packet 2b).

The tasur-site script translates a flat English catalog (``catalog.py``'s
``EN`` dict) and writes a parallel ``i18n/strings.json``. This app instead
uses gettext ``.po`` files where the msgid *is* the English source string,
so there's no separate catalog module to read from — the source text lives
in the .po entries themselves (``make i18n-extract``/``i18n-update`` already
populated them from ``{{ _("...") }}`` template call sites).

Endpoint: POST https://api.tartunlp.ai/translation/v2
Payload:  {"text": <str|list>, "src": "en", "tgt": "et"|"ru"}

Workflow, per locale (et, ru):
  1. Load the locale's messages.po. Untranslated = non-empty msgid, empty
     msgstr (skips the header entry and anything already translated by a
     human — this script never overwrites a non-fuzzy human translation).
  2. Protect statutory/brand terms with placeholder tokens before sending
     text off-box (PROTECTED below — KMD, TSD, registrikood, Tasur,
     SAE Books, matching the scope's named protect-list).
  3. Translate EN -> target in polite sequential batches.
  4. Back-translate target -> EN as a QA artefact (i18n/backtranslation-qa.json).
  5. Write the translated msgstr back into the .po, flagged ``fuzzy`` —
     MT output is a draft; fuzzy is gettext's own "needs human review"
     marker, so no translated string silently masquerades as reviewed.

Never hand-edit a fuzzy entry's msgid to "fix" a bad translation: fix the
English source template string and re-run. Re-run after `make i18n-update`
picks up new/changed msgids.

Usage:
    python tools/translate_po.py            # translate every untranslated
                                              # msgid in et + ru catalogs
    python tools/translate_po.py --full      # ignore nothing (same as
                                              # default; this script only
                                              # ever touches untranslated
                                              # entries — --full is accepted
                                              # for interface parity with
                                              # tasur-site's translate.py
                                              # but has no distinct effect
                                              # here, since there is no
                                              # separate "reuse cache" like
                                              # strings.json to bypass)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po, write_po

API = "https://api.tartunlp.ai/translation/v2"
ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "saebooks_web" / "i18n" / "locales"
QA_PATH = ROOT / "i18n" / "backtranslation-qa.json"
DOMAIN = "messages"
BATCH = 20

#: Protect-list per the scope's named terms. Order matters: nothing here
#: is a substring of another entry, but keep longest-first as a habit
#: carried over from tasur-site's catalog.py in case terms are added later.
#:
#: Fixer round 4: KMV was entirely absent (only KMD was listed — a
#: different statutory term) and matching was case-sensitive literal
#: substring, so capitalised sentence-start occurrences of "Registrikood"
#: (the field label, the two validation errors) went through TartuNLP
#: unprotected and came back mistranslated (verified: messages.mo's ru
#: catalog had msgid "Registrikood" -> msgstr "Регистрация"). protect()/
#: unprotect() below now match case-insensitively and restore whichever
#: casing was actually present (see _CAP_SUFFIX), so both "registrikood"
#: and "Registrikood" round-trip untranslated.
PROTECTED: list[tuple[str, str]] = [
    ("SAE Books", "NX1"),
    ("Tasur", "NX2"),
    ("registrikood", "NX3"),
    ("KMD", "NX4"),
    ("TSD", "NX5"),
    # gettext %-format placeholders — MT mangles "%(brand)s" (spaces get
    # inserted, "brand" gets partially translated) unless it's protected
    # exactly like a statutory term. Add one entry per placeholder name
    # used in a msgid; a plain word token round-trips through MT intact
    # where the punctuation-heavy "%(name)s" does not.
    ("%(brand)s", "NX6"),
    ("KMV", "NX7"),
]

#: Locales sent to TartuNLP as MT targets.
TARGET_LOCALES: tuple[str, ...] = ("et", "ru")

#: "en" is the source language — never machine-translated, but its own
#: catalog still needs msgstr filled (identity: msgstr == msgid) so the
#: compiled .mo carries every msgid the .po does. Without this, an empty
#: msgstr compiles to NO entry at all (pybabel drops empty translations),
#: which breaks the po/mo msgid-set parity check in test_i18n_compile.py.
#: Never flagged fuzzy — an identity translation isn't a draft needing
#: review, it's definitionally correct.
IDENTITY_LOCALE = "en"


def api_translate(texts: list[str], src: str, tgt: str) -> list[str]:
    body = json.dumps({"text": texts, "src": src, "tgt": tgt}).encode()
    req = urllib.request.Request(
        API, data=body, headers={"Content-Type": "application/json"}
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read())["result"]
            return out if isinstance(out, list) else [out]
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} after error: {exc}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")  # pragma: no cover


#: Suffix appended to a term's token when the matched occurrence was
#: Title-cased (first letter upper) but the PROTECTED entry itself is
#: lowercase — e.g. "Registrikood" at a sentence start vs the entry
#: "registrikood". unprotect() strips the suffix and re-capitalises the
#: restored term accordingly. A suffix (not a distinct numeric token) so
#: tokens_lost()'s plain ``token in text`` substring check still finds the
#: base token inside "NX3CAP" without needing its own PROTECTED entry.
_CAP_SUFFIX = "CAP"


#: Generic python-format protection — EVERY %(name)s / %(name)d token and
#: literal %% is swapped for an inert numbered token before MT (the static
#: PROTECTED list only ever covered %(brand)s; TartuNLP translates the
#: placeholder NAME otherwise: "%(date)s" -> "% (kuupäev) s", which is an
#: invalid format spec and a runtime ValueError on render). Tokens are plain
#: uppercase words, which round-trip MT intact.
_PYFMT = re.compile(r"%\((\w+)\)[sd]|%%")


def _protect_pyfmt(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}

    def _sub(m: "re.Match[str]") -> str:
        token = f"ZQX{len(mapping)}Z"
        mapping[token] = m.group(0)
        return token

    return _PYFMT.sub(_sub, text), mapping


def _unprotect_pyfmt(text: str, mapping: dict[str, str]) -> str:
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text


def protect(text: str) -> str:
    for term, token in PROTECTED:
        pattern = re.compile(re.escape(term), re.IGNORECASE)

        def _sub(match: re.Match[str], term: str = term, token: str = token) -> str:
            matched = match.group(0)
            if term[:1].islower() and matched[:1].isupper():
                return token + _CAP_SUFFIX
            return token

        text = pattern.sub(_sub, text)
    return text


def unprotect(text: str) -> str:
    for term, token in PROTECTED:
        capitalised = term[:1].upper() + term[1:] if term else term
        # Longer/suffixed form first — token is a substring of token+CAP.
        text = text.replace(token + _CAP_SUFFIX, capitalised)
        text = text.replace(token, term)
    return text


def tidy(text: str) -> str:
    """Typography normalisation only — never rewording."""
    text = text.replace(" - ", " — ")
    return text.strip()


def tokens_lost(src: str, raw: str) -> list[tuple[str, str]]:
    return [(term, token) for term, token in PROTECTED if token in src and token not in raw]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p for p in parts if p.strip()]


def translate_batch(sources: list[str], tgt: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Translate already-protected ``sources`` EN -> ``tgt``.

    Returns (results, leaks) where leaks is a list of (token, term) pairs
    for any placeholder that didn't survive even the per-sentence retry.
    """
    out: list[str] = []
    for i in range(0, len(sources), BATCH):
        chunk = sources[i : i + BATCH]
        out.extend(api_translate(chunk, "en", tgt))
        print(f"  en->{tgt}: {min(i + BATCH, len(sources))}/{len(sources)}")
        time.sleep(1.0)  # politeness — university research service

    results: list[str] = []
    leaks: list[tuple[str, str]] = []
    for src, raw in zip(sources, out):
        lost = tokens_lost(src, raw)
        if lost:
            sentences = _split_sentences(src)
            if len(sentences) > 1:
                retried = api_translate(sentences, "en", tgt)
                candidate = " ".join(retried)
                time.sleep(1.0)
                if not tokens_lost(src, candidate):
                    raw = candidate
                    lost = []
        leaks.extend(lost)
        results.append(tidy(unprotect(raw)))
    return results, leaks


def back_translate(texts: list[str], src_locale: str) -> list[str]:
    protected = [protect(t) for t in texts]
    out: list[str] = []
    for i in range(0, len(protected), BATCH):
        out.extend(api_translate(protected[i : i + BATCH], src_locale, "en"))
        print(f"  {src_locale}->en (QA): {min(i + BATCH, len(protected))}/{len(protected)}")
        time.sleep(1.0)
    return [tidy(unprotect(t)) for t in out]


def _load_catalog(locale: str) -> Catalog:
    po_path = LOCALES_DIR / locale / "LC_MESSAGES" / f"{DOMAIN}.po"
    with open(po_path, "rb") as fh:
        return read_po(fh, locale=locale, domain=DOMAIN)


def _write_catalog(locale: str, catalog: Catalog) -> None:
    po_path = LOCALES_DIR / locale / "LC_MESSAGES" / f"{DOMAIN}.po"
    with open(po_path, "wb") as fh:
        write_po(fh, catalog, sort_output=False, ignore_obsolete=True)


def _untranslated_ids(catalog: Catalog) -> list[str]:
    """msgids with no msgstr yet — the header (id == '') is never included."""
    return [m.id for m in catalog if m.id and not m.string]


def translate_locale(locale: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    catalog = _load_catalog(locale)
    ids = _untranslated_ids(catalog)
    if not ids:
        print(f"[{locale}] nothing untranslated")
        return {}, []

    print(f"[{locale}] {len(ids)} untranslated msgid(s)")
    pyfmt_pairs = [_protect_pyfmt(msgid) for msgid in ids]
    protected_sources = [protect(t) for t, _m in pyfmt_pairs]
    translated, leaks_tokens = translate_batch(protected_sources, locale)
    leaks = [(locale, term) for term, _tok in leaks_tokens]
    translated = [
        _unprotect_pyfmt(t, m) for t, (_src, m) in zip(translated, pyfmt_pairs)
    ]

    for msgid, msgstr in zip(ids, translated):
        message = catalog.get(msgid)
        if message is None:  # pragma: no cover — defensive
            continue
        message.string = msgstr
        message.flags.add("fuzzy")  # MT draft — needs native-speaker review

    _write_catalog(locale, catalog)
    return dict(zip(ids, translated)), leaks


def fill_identity_locale(locale: str) -> int:
    """Set msgstr = msgid for every untranslated entry in ``locale``'s catalog.

    Returns the number of entries filled. Not machine translation — this is
    the source language, so "translation" is the string itself.
    """
    catalog = _load_catalog(locale)
    ids = _untranslated_ids(catalog)
    for msgid in ids:
        message = catalog.get(msgid)
        if message is not None:
            message.string = msgid
    if ids:
        _write_catalog(locale, catalog)
    return len(ids)


def main() -> None:
    all_leaks: list[tuple[str, str]] = []
    qa: dict[str, dict[str, str]] = {}

    filled = fill_identity_locale(IDENTITY_LOCALE)
    if filled:
        print(f"[{IDENTITY_LOCALE}] filled {filled} identity msgstr(s)")
    else:
        print(f"[{IDENTITY_LOCALE}] nothing untranslated")

    for locale in TARGET_LOCALES:
        fresh, leaks = translate_locale(locale)
        all_leaks.extend(leaks)
        if fresh:
            back = back_translate(list(fresh.values()), locale)
            qa[locale] = {
                msgid: back_text
                for msgid, back_text in zip(fresh.keys(), back)
            }

    qa_out = {
        "note": (
            "Back-translations (TartuNLP <locale>->en) for QA review of "
            "fuzzy-flagged .po entries written by tools/translate_po.py. "
            "Where a back-translation diverges materially from the English "
            "msgid, a native speaker should review the .po entry directly "
            "(it carries the #, fuzzy flag) rather than editing this file."
        ),
        "engine": "TartuNLP / Neurotõlge, University of Tartu — https://api.tartunlp.ai/translation/v2",
        **{f"{locale}_back": entries for locale, entries in qa.items()},
    }
    QA_PATH.parent.mkdir(exist_ok=True)
    QA_PATH.write_text(json.dumps(qa_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if all_leaks:
        print("\nWARNING — placeholder tokens lost in translation (fix source and re-run):")
        for locale, term in all_leaks:
            print(f"  [{locale}] {term}")
    else:
        print("\nAll placeholder tokens survived translation (or none were present).")
    print(f"Wrote {QA_PATH.relative_to(ROOT)}")
    print("Run `make i18n-compile` to rebuild .mo from the updated .po files.")


if __name__ == "__main__":
    main()
