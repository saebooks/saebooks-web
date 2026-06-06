"""Regression guard for the 2026-05-25 contact type-alias bug.

The /api/v1/contacts endpoint reads ``?type=`` (Query alias), not
``?contact_type=``. FastAPI silently ignores unknown query params, so
sending ``contact_type=`` returns ALL contacts unfiltered — and the
CUSTOMER picker on /bills/new was therefore accepting customers as
suppliers (and symmetric on /invoices/new). 22 GET callers were fixed;
this test fires if anyone reintroduces the wrong param name in a
``params={...}`` dict.

POST bodies that legitimately use ``"contact_type": "..."`` as the
model field name (e.g. one-off contact creation in bills.py:380-385
and invoices.py:358-363) are NOT affected — they pass JSON to a
separate endpoint that takes the model field directly.
"""
from __future__ import annotations

import re
from pathlib import Path

ROUTES_DIR = Path(__file__).resolve().parent.parent / "saebooks_web" / "routes"
# Match params={"contact_type": ...} — i.e. dict-form starting immediately
# after params=. POST bodies use json={...} and won't match.
BAD_PATTERN = re.compile(r'params=\{[\'"]contact_type[\'"]\s*:')


def test_no_route_uses_contact_type_in_params() -> None:
    offenders: list[str] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        src = path.read_text()
        for lineno, line in enumerate(src.splitlines(), start=1):
            if BAD_PATTERN.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Contact type-alias regression: /api/v1/contacts expects "
        "?type=, NOT ?contact_type=. Use params={\"type\": ...}.\n"
        + "\n".join(offenders)
    )
