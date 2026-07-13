# i18n wiring — EE GUI prep Packet 2a.
#
# Domain "messages", locales et/en/ru live under saebooks_web/i18n/locales/
# (see saebooks_web/i18n/__init__.py — DOMAIN, LOCALES_DIR,
# SUPPORTED_LOCALES). Extraction config is babel.cfg at repo root.
#
# Workflow:
#   make i18n-extract   # templates+.py -> i18n/messages.pot (source catalog)
#   make i18n-update     # merge new/changed msgids into each locale's .po
#   make i18n-compile    # .po -> .mo (what actually gets loaded at runtime)
#
# Run extract+update after adding/changing any {% trans %}/_()/gettext()
# call site; run compile before running the app or tests (compiled .mo
# files are what saebooks_web.i18n.gettext reads — see the
# compile-in-CI test in tests/test_i18n_compile.py, which fails loudly if
# a .po has drifted ahead of its .mo).

LOCALES_DIR := saebooks_web/i18n/locales
POT := i18n/messages.pot
LOCALES := et en ru

.PHONY: i18n-extract i18n-init i18n-update i18n-compile

i18n-extract:
	mkdir -p i18n
	.venv/bin/python tools/extract_pot.py  # babel API — CLI skips _partials/_components (see tools/extract_pot.py docstring)

i18n-init: i18n-extract
	@for locale in $(LOCALES); do \
		if [ ! -f $(LOCALES_DIR)/$$locale/LC_MESSAGES/messages.po ]; then \
			pybabel init -i $(POT) -d $(LOCALES_DIR) -l $$locale -D messages; \
		fi; \
	done

i18n-update: i18n-extract
	@for locale in $(LOCALES); do \
		pybabel update -i $(POT) -d $(LOCALES_DIR) -l $$locale -D messages --no-wrap; \
	done

i18n-compile:
	# --use-fuzzy: tools/translate_po.py writes MT drafts flagged "fuzzy" (the
	# QA-pending marker) — without this flag pybabel silently DROPS fuzzy
	# entries from the .mo, which (a) serves English instead of the MT draft
	# and (b) breaks the po/mo msgid-set parity check in test_i18n_compile.py.
	# Ship the MT draft; the fuzzy flag stays in .po as the review todo.
	pybabel compile -d $(LOCALES_DIR) -D messages --use-fuzzy --statistics
