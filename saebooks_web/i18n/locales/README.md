# Translations — machine-generated, and we'd love your help

**Please read this before trusting a non-English translation.**

Every non-English translation in SAE Books / Tasur — Estonian (`et`),
Russian (`ru`), and any others — is **machine-generated** (via
[TartuNLP](https://tartunlp.ai/)) and has **not been reviewed or validated by
native speakers or by SAE**. **English is the source of truth.** Translated
strings may contain errors, awkward phrasing, or the wrong accounting/tax
terminology. Do not rely on a translated label as authoritative — if a
translated term and the English disagree, the English wins.

We are honest about this on purpose. We would rather ship an imperfect
translation you can help fix than pretend we speak a language we don't.

## Help improve a translation — contributions are very welcome

If you are a fluent or native speaker, corrections are the single most valuable
thing you can contribute. You do **not** need to be a programmer.

1. Open `locales/<lang>/LC_MESSAGES/messages.po` (e.g. `locales/et/LC_MESSAGES/messages.po`).
2. Find the `msgid` (the English source) and fix its `msgstr` (the translation).
3. **Keep every placeholder and variable intact** — things like `%(amount)s`,
   `{count}`, and HTML tags must appear unchanged in your translation, or the
   app will break. (Our tooling checks this.)
4. Open a pull request. A maintainer will review it.

Your corrections are **protected**: the machine-translation pipeline
(`tools/translate_po.py`) never overwrites a human-reviewed string, so a fix you
contribute stays fixed on the next regeneration.

Prefer not to edit files? Open an **issue** describing the wrong string (the
English text, the language, and a better translation) and we'll apply it.

## The fine print

Contributions are under **AGPL-3.0** and require the one-time **SAE Books CLA**
— see [`CONTRIBUTING.md`](../../../CONTRIBUTING.md). Terminology accuracy for tax
and accounting terms matters more than literal wordiness; when in doubt, match
how the local tax authority words it.
