"""Shared form-parsing helpers used by invoice and bill create routes."""
from __future__ import annotations

_LINE_FIELDS = ("account_id", "description", "quantity", "unit_price", "tax_code_id", "item_id", "margin_acq_cost", "project_id", "tracking_vehicle_id")


def parse_lines(form: dict[str, str]) -> list[dict[str, object]]:
    """Extract line-item dicts from a flat form dict.

    Convention: fields are named ``lines[N][field]`` where N is a zero-based
    integer index.  We collect all indices found, then build one dict per index.
    Missing optional fields (tax_code_id, item_id) are omitted when blank.
    """
    # Discover all index values present.
    indices: set[int] = set()
    for key in form:
        if key.startswith("lines[") and "][" in key:
            try:
                idx = int(key.split("[")[1].split("]")[0])
                indices.add(idx)
            except (ValueError, IndexError):
                pass

    lines: list[dict[str, object]] = []
    for idx in sorted(indices):
        line: dict[str, object] = {}
        for field in _LINE_FIELDS:
            val = form.get(f"lines[{idx}][{field}]", "").strip()
            if val:
                line[field] = val
        if line:
            lines.append(line)
    return lines
