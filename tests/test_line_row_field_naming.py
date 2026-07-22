"""Every ``_line_row.html`` must name fields the way parse_lines reads them.

``form_helpers.parse_lines`` discovers line indices by testing for ``"]["`` in
the form key, i.e. it only ever sees ``lines[N][field]``. The expenses partial
used to render ``lines[N]_field``, which matches nothing — so every expense
created through the UI was saved with ZERO lines and no error anywhere: the
POST returned 201, the record existed, the money did not. Found 2026-07-23 on
demo.tasur.ee.

This is a naming contract between templates and one shared parser, so it is
worth asserting directly rather than via a request-level test per entity.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from saebooks_web.form_helpers import parse_lines

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_LINE_ROWS = sorted(_TEMPLATES.glob("*/_line_row.html"))
_LINE_FIELD = re.compile(r'name="(lines\[[^"]*)"')


def test_line_row_partials_were_found() -> None:
    """Guard the guard — a bad glob would make every test below vacuous."""
    assert _LINE_ROWS, f"no */_line_row.html under {_TEMPLATES}"


@pytest.mark.parametrize("path", _LINE_ROWS, ids=lambda p: p.parent.name)
def test_line_fields_use_bracket_convention(path: Path) -> None:
    names = _LINE_FIELD.findall(path.read_text())
    assert names, f"{path} renders no lines[...] fields"
    for name in names:
        assert "][" in name, (
            f"{path.parent.name}/_line_row.html renders {name!r}. parse_lines "
            "keys off '][', so this field is silently dropped and the record "
            "saves with no lines. Use lines[{{ index }}][field]."
        )


def test_parse_lines_ignores_the_underscore_spelling() -> None:
    """Pin the failure mode itself, so the reason for the rule stays visible."""
    broken = {
        "lines[0]_description": "Widget",
        "lines[0]_quantity": "2",
        "lines[0]_unit_price": "50.00",
    }
    assert parse_lines(broken) == [], (
        "parse_lines silently returning [] for the underscore spelling is "
        "exactly what made this bug invisible"
    )


def test_parse_lines_reads_the_bracket_spelling() -> None:
    good = {
        "lines[0][description]": "Widget",
        "lines[0][quantity]": "2",
        "lines[0][unit_price]": "50.00",
    }
    assert parse_lines(good) == [
        {"description": "Widget", "quantity": "2", "unit_price": "50.00"}
    ]
