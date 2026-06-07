"""agent/tools.py — GUI dev-console function-calling tools for saebooks-web.

This is a trimmed port of the estonia-planner agent tools: it keeps ONLY the
live-GUI editor tools and drops every trip-specific tool. The dev console
(saebooks_web.routes.dev, mode='dev') uses these to read and edit the app's
own Jinja2 templates and static assets, with the live preview reloading after
each write.

Tools:
  gui_list_files(subdir)              — list editable templates/ + static/ files
  gui_read_file(path)                 — read one file (ALWAYS read before editing)
  gui_replace(path, find, replace)    — targeted substring replacement (preferred)
  gui_write_file(path, content)       — overwrite / create a file

SECURITY: every path is resolved through ``_gui_resolve`` and rejected unless it
lands inside ``templates/`` or ``static/`` at the repo root. ``..`` traversal,
absolute escapes, and symlink escapes (via ``Path.resolve()``) are rejected.
There is no path that reaches backend ``.py`` source.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI-schema tool definitions (passed to the chat completions API)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "gui_list_files",
            "description": (
                "List the editable GUI source files of THIS app — Jinja2 templates "
                "(templates/**, .html) and static assets (static/**, .css/.js/.svg). "
                "Returns relative paths to pass to gui_read_file / gui_replace / "
                "gui_write_file. Call this first to discover the file you need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": "Optional: limit to 'templates' or 'static'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gui_read_file",
            "description": (
                "Read the full text of one GUI file. Path is relative to the repo root, "
                "e.g. 'templates/dashboard.html' or 'static/js/app.js'. "
                "ALWAYS read a file before editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under templates/ or static/.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gui_replace",
            "description": (
                "Make a targeted edit to a GUI file by replacing an exact substring. "
                "Preferred over gui_write_file for small changes — safer, preserves the rest "
                "of the file. 'find' must match exactly (incl. whitespace). Changes reflect "
                "LIVE on the next page render."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under templates/ or static/.",
                    },
                    "find": {"type": "string", "description": "Exact text to find."},
                    "replace": {"type": "string", "description": "Replacement text."},
                    "count": {
                        "type": "integer",
                        "description": "Max replacements (default 1; pass a large number for all).",
                    },
                },
                "required": ["path", "find", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gui_write_file",
            "description": (
                "Overwrite (or create) a GUI file with full new content. Use for new files or "
                "large rewrites; prefer gui_replace for small edits. Only paths under "
                "templates/ or static/ are allowed. Changes reflect LIVE on the next render — "
                "keep templates valid Jinja2/HTML so the app keeps working."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under templates/ or static/.",
                    },
                    "content": {"type": "string", "description": "Full new file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# DEV GUI editor tools — live-edit this app's own templates + static assets.
#
# Scoped strictly to the repo-root templates/ and static/ dirs; path traversal
# is rejected. NOTE on layout: in estonia-planner templates/static lived under
# app/, so it used parent.parent. In saebooks-web this module is at
# saebooks_web/agent/tools.py while templates/ + static/ live at the REPO ROOT
# (one level above the package), so we walk up THREE parents:
#   tools.py -> agent/ -> saebooks_web/ -> <repo root>
# In the container the repo root is /app, with /app/templates and /app/static.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GUI_ROOTS = (_REPO_ROOT / "templates", _REPO_ROOT / "static")
_GUI_SKIP = {"__pycache__", ".DS_Store"}
_GUI_EXTS = {".html", ".css", ".js", ".svg", ".json", ".txt", ".webmanifest"}
_GUI_MAX_BYTES = 512_000


def _gui_resolve(path: str) -> Path:
    """Resolve a user path under templates/ or static/, rejecting traversal/escape."""
    rel = (path or "").strip().lstrip("/")
    target = (_REPO_ROOT / rel).resolve()
    for root in _GUI_ROOTS:
        if target == root or root in target.parents:
            return target
    raise ValueError(
        f"Path {path!r} is outside the editable GUI roots (templates/, static/)."
    )


def gui_list_files(subdir: str | None = None) -> dict[str, Any]:
    """List editable GUI files (relative paths) under templates/ and static/."""
    roots = _GUI_ROOTS
    if subdir in ("templates", "static"):
        roots = (_REPO_ROOT / subdir,)
    files: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _GUI_EXTS:
                continue
            if any(part in _GUI_SKIP for part in p.parts):
                continue
            files.append(str(p.relative_to(_REPO_ROOT)))
    return {"root": ".", "count": len(files), "files": files}


def gui_read_file(path: str) -> dict[str, Any]:
    """Return the full text of one GUI file."""
    target = _gui_resolve(path)
    if not target.is_file():
        return {"error": f"Not found: {path}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": str(target.relative_to(_REPO_ROOT)), "bytes": len(text), "content": text}


def gui_write_file(path: str, content: str) -> dict[str, Any]:
    """Overwrite or create a GUI file with new content."""
    if content is None:
        return {"error": "content is required"}
    if len(content.encode("utf-8")) > _GUI_MAX_BYTES:
        return {"error": f"content exceeds {_GUI_MAX_BYTES} bytes"}
    target = _gui_resolve(path)
    existed = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("GUI %s file %s (%d bytes)", "updated" if existed else "created", path, len(content))
    return {
        "path": str(target.relative_to(_REPO_ROOT)),
        "bytes": len(content),
        "action": "updated" if existed else "created",
    }


def gui_replace(path: str, find: str, replace: str, count: int = 1) -> dict[str, Any]:
    """Replace an exact substring in a GUI file (targeted edit)."""
    target = _gui_resolve(path)
    if not target.is_file():
        return {"error": f"Not found: {path}"}
    if not find:
        return {"error": "find text is required"}
    text = target.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(find)
    if occurrences == 0:
        return {"error": "find text not found — read the file first and match exactly (incl. whitespace)."}
    n = occurrences if (count is None or count <= 0) else count
    target.write_text(text.replace(find, replace, n), encoding="utf-8")
    replaced = min(n, occurrences)
    logger.info("GUI replace in %s: %d occurrence(s)", path, replaced)
    return {
        "path": str(target.relative_to(_REPO_ROOT)),
        "replaced": replaced,
        "occurrences_total": occurrences,
    }


# ---------------------------------------------------------------------------
# GUI dev tools that mutate files — the router emits a 'gui_changed' SSE after
# these so the dev console can hot-reload its live preview iframe.
# ---------------------------------------------------------------------------

GUI_WRITE_TOOLS: frozenset[str] = frozenset({"gui_write_file", "gui_replace"})


# ---------------------------------------------------------------------------
# Dispatcher — called by the router after tool_calls arrive.
# All gui_* tools are synchronous (plain disk IO), so dispatch is sync; the
# router awaits nothing here.
# ---------------------------------------------------------------------------

def dispatch(tool_name: str, arguments_json: str) -> str:
    """Parse tool arguments and call the appropriate gui_* function.

    Returns a JSON string to send back as the tool result.
    """
    try:
        args: dict = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}

    try:
        if tool_name == "gui_list_files":
            result = gui_list_files(subdir=args.get("subdir"))
        elif tool_name == "gui_read_file":
            result = gui_read_file(path=args["path"])
        elif tool_name == "gui_write_file":
            result = gui_write_file(path=args["path"], content=args.get("content", ""))
        elif tool_name == "gui_replace":
            result = gui_replace(
                path=args["path"],
                find=args.get("find", ""),
                replace=args.get("replace", ""),
                count=int(args.get("count", 1) or 1),
            )
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as exc:
        logger.error("Tool %s raised: %s", tool_name, exc, exc_info=True)
        result = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False)
