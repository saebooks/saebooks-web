"""agent/tools.py — dev-console function-calling tools for saebooks-web.

Two tiers of tools live here:

1. **GUI editor tools** (``gui_*``) — ALWAYS available. A trimmed port of the
   estonia-planner agent tools: they read and edit the app's own Jinja2
   templates and static assets only, path-guarded to ``templates/`` and
   ``static/`` at the repo root. The live preview reloads after each write.

2. **Full-CLI tools** (``bash``, ``read_file``, ``write_file``, ``edit_file``) —
   available ONLY when the ``DEV_CONSOLE_FULL_CLI`` env flag is truthy. These give
   the agent a normal Claude-CLI-style surface: run shell commands and read/write/
   edit ANY file inside the container. This is for the owner-gated saebooks-dev-
   edition stack; in PROD the flag is unset and these tools are neither advertised
   to the model nor dispatchable (a direct call is rejected). See
   ``full_cli_enabled()`` for the single source of truth on the gate.

SECURITY (gui_*): every path is resolved through ``_gui_resolve`` and rejected
unless it lands inside ``templates/`` or ``static/`` at the repo root. ``..``
traversal, absolute escapes, and symlink escapes (via ``Path.resolve()``) are
rejected. There is no gui_* path that reaches backend ``.py`` source.

SECURITY (full-CLI): there is NO path guard — by design these are full-power
tools. The ONLY guard is the env flag + the owner gate on the /dev endpoints
(routes/dev.py: _require_auth + _is_sae_staff). Blast radius = the dev-edition
container's filesystem and whatever the bosun network exposes to it. Never set
``DEV_CONSOLE_FULL_CLI`` on the production saebooks-web stack.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Full-CLI gate — SINGLE source of truth.
#
# The full-CLI tools (bash/read_file/write_file/edit_file) are exposed to the
# model and made dispatchable ONLY when this returns True. It reads the env
# var at call time (not import time) so tests can toggle it and so a restart
# with the flag set picks it up. PROD leaves DEV_CONSOLE_FULL_CLI unset, so the
# agent only ever sees the gui_* tools there and a stray 'bash' call is rejected.
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}


def full_cli_enabled() -> bool:
    """True iff DEV_CONSOLE_FULL_CLI is set to a truthy value ('1'/'true'/'yes'/'on')."""
    return os.environ.get("DEV_CONSOLE_FULL_CLI", "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# OpenAI-schema tool definitions (passed to the chat completions API)
# ---------------------------------------------------------------------------

# GUI editor tools — ALWAYS advertised to the model.
GUI_TOOL_DEFINITIONS: list[dict] = [
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

# Full-CLI tools — advertised to the model ONLY when full_cli_enabled().
FULL_CLI_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command inside the dev-edition container and return its "
                "stdout, stderr and exit code. This is a full shell — use it like a "
                "terminal (ls, cat, grep, git, python, pip, curl, etc.). Output is "
                "captured and large output is truncated. Long-running commands are "
                "killed at the timeout. Blast radius is the container filesystem and "
                "the bosun network the container is attached to."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command line to execute (run via /bin/sh -c).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds before the command is killed (default 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the full text of ANY file in the container by path (absolute, "
                "e.g. '/app/saebooks_web/main.py', or repo-relative, e.g. "
                "'saebooks_web/routes/dev.py'). Unlike gui_read_file this is not "
                "restricted to templates/ + static/. ALWAYS read before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative path to the file.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write (create or overwrite) ANY file in the container with full new "
                "content. Parent directories are created as needed. Absolute or "
                "repo-relative path. Not restricted to templates/ + static/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative path to the file.",
                    },
                    "content": {"type": "string", "description": "Full new file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact substring in ANY file in the container. 'find' must "
                "match exactly (incl. whitespace). Not restricted to templates/ + "
                "static/. Prefer this over write_file for small edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative path to the file.",
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
]


def tool_definitions() -> list[dict]:
    """Return the tool schemas the agent should see for THIS request.

    Always includes the gui_* tools. Includes the full-CLI tools ONLY when
    ``full_cli_enabled()``. This is the list the /dev chat loop passes to the
    model, so with the flag off the model is never told the full-CLI tools exist.
    """
    if full_cli_enabled():
        return GUI_TOOL_DEFINITIONS + FULL_CLI_TOOL_DEFINITIONS
    return list(GUI_TOOL_DEFINITIONS)


# Back-compat alias: some callers/tests import TOOL_DEFINITIONS directly. This is
# the gui-only baseline; the live agent loop uses tool_definitions() instead so
# the full-CLI tools are gated. Kept as the gui_* list to preserve old behaviour.
TOOL_DEFINITIONS: list[dict] = GUI_TOOL_DEFINITIONS


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
# FULL-CLI tools — bash + arbitrary file IO. Gated by full_cli_enabled().
#
# These are deliberately UNGUARDED w.r.t. path/command — that is the whole point
# of the dev-edition console. The guard is the env flag + the owner gate on the
# /dev endpoints. Every invocation is audit-logged at WARNING with the
# 'devconsole-fullcli' marker so actions are traceable in the container logs.
# ---------------------------------------------------------------------------

# Marker grepped for in container logs to trace full-CLI activity.
_AUDIT_MARKER = "devconsole-fullcli"

# Output cap so a runaway command (e.g. `cat huge.bin`) can't blow up the
# response / the model context.
_FULLCLI_MAX_OUTPUT = 60_000
_FULLCLI_MAX_WRITE_BYTES = 5_000_000
_DEFAULT_BASH_TIMEOUT = 120


def _audit(action: str, detail: str) -> None:
    """Append an audit line for a full-CLI action (WARNING level, marker-tagged)."""
    logger.warning("%s action=%s %s", _AUDIT_MARKER, action, detail)


def _truncate(text: str, limit: int = _FULLCLI_MAX_OUTPUT) -> tuple[str, bool]:
    """Truncate text to ``limit`` bytes-ish (chars), flagging if it was cut."""
    if len(text) <= limit:
        return text, False
    head = limit - 200
    return (
        text[:head] + f"\n…[truncated {len(text) - head} chars]…",
        True,
    )


def _resolve_any(path: str) -> Path:
    """Resolve an absolute or repo-relative path WITHOUT a sandbox guard."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw)
    if not p.is_absolute():
        p = _REPO_ROOT / raw
    return p


def bash(command: str, timeout: int = _DEFAULT_BASH_TIMEOUT) -> dict[str, Any]:
    """Run ``command`` in a shell inside the container; capture stdout+stderr+rc."""
    if not full_cli_enabled():
        return {"error": "full-CLI disabled (DEV_CONSOLE_FULL_CLI not set)"}
    cmd = (command or "").strip()
    if not cmd:
        return {"error": "command is required"}
    try:
        to = int(timeout) if timeout else _DEFAULT_BASH_TIMEOUT
    except (TypeError, ValueError):
        to = _DEFAULT_BASH_TIMEOUT
    to = max(1, min(to, 600))

    _audit("bash", f"timeout={to}s command={cmd!r}")
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=to,
            cwd=str(_REPO_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        out, _ = _truncate(exc.stdout or "" if isinstance(exc.stdout, str) else "")
        return {
            "error": f"timed out after {to}s",
            "exit_code": None,
            "stdout": out,
            "stderr": "(killed on timeout)",
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {"error": f"failed to run: {exc}"}

    stdout, out_trunc = _truncate(proc.stdout or "")
    stderr, err_trunc = _truncate(proc.stderr or "")
    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_trunc or err_trunc,
    }


def read_file(path: str) -> dict[str, Any]:
    """Read any file in the container (absolute or repo-relative)."""
    if not full_cli_enabled():
        return {"error": "full-CLI disabled (DEV_CONSOLE_FULL_CLI not set)"}
    target = _resolve_any(path)
    if not target.is_file():
        return {"error": f"Not found: {path}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    out, truncated = _truncate(text)
    return {"path": str(target), "bytes": len(text), "content": out, "truncated": truncated}


def write_file(path: str, content: str) -> dict[str, Any]:
    """Write any file in the container (creates parent dirs)."""
    if not full_cli_enabled():
        return {"error": "full-CLI disabled (DEV_CONSOLE_FULL_CLI not set)"}
    if content is None:
        return {"error": "content is required"}
    if len(content.encode("utf-8")) > _FULLCLI_MAX_WRITE_BYTES:
        return {"error": f"content exceeds {_FULLCLI_MAX_WRITE_BYTES} bytes"}
    target = _resolve_any(path)
    existed = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _audit("write_file", f"path={target} bytes={len(content)} action={'updated' if existed else 'created'}")
    return {
        "path": str(target),
        "bytes": len(content),
        "action": "updated" if existed else "created",
    }


def edit_file(path: str, find: str, replace: str, count: int = 1) -> dict[str, Any]:
    """Replace an exact substring in any file in the container."""
    if not full_cli_enabled():
        return {"error": "full-CLI disabled (DEV_CONSOLE_FULL_CLI not set)"}
    target = _resolve_any(path)
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
    _audit("edit_file", f"path={target} replaced={replaced}")
    return {
        "path": str(target),
        "replaced": replaced,
        "occurrences_total": occurrences,
    }


# ---------------------------------------------------------------------------
# Mutating-tool sets.
#
# GUI_WRITE_TOOLS — the router emits a 'gui_changed' SSE after these so the dev
# console hot-reloads its live-preview iframe.
#
# FULL_CLI_TOOLS — the full-power tools, gated by full_cli_enabled().
# MUTATING_TOOLS — every tool that changes state on disk or runs a command
# (gui writes + full-CLI writes/edits + bash). Used for audit/UI feedback.
# ---------------------------------------------------------------------------

GUI_WRITE_TOOLS: frozenset[str] = frozenset({"gui_write_file", "gui_replace"})
FULL_CLI_TOOLS: frozenset[str] = frozenset({"bash", "read_file", "write_file", "edit_file"})
# Mutating = anything that writes to disk or executes a command.
MUTATING_TOOLS: frozenset[str] = GUI_WRITE_TOOLS | frozenset(
    {"bash", "write_file", "edit_file"}
)


def available_tool_names() -> frozenset[str]:
    """Names of tools that are dispatchable for THIS request (flag-aware)."""
    gui = {t["function"]["name"] for t in GUI_TOOL_DEFINITIONS}
    if full_cli_enabled():
        return frozenset(gui | FULL_CLI_TOOLS)
    return frozenset(gui)


# ---------------------------------------------------------------------------
# Dispatcher — called by the router after tool_calls arrive.
# All tools here are synchronous (disk IO / subprocess), so dispatch is sync.
# ---------------------------------------------------------------------------

def dispatch(tool_name: str, arguments_json: str) -> str:
    """Parse tool arguments and call the appropriate tool function.

    Returns a JSON string to send back as the tool result. Full-CLI tools are
    rejected here when the flag is off — defence in depth on top of not
    advertising them to the model.
    """
    try:
        args: dict = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}

    # Hard gate: even if a (compromised / confused) model emits a full-CLI tool
    # call when the flag is off, refuse it here. The schema list already omits
    # them, so this is belt-and-braces.
    if tool_name in FULL_CLI_TOOLS and not full_cli_enabled():
        logger.warning(
            "%s REJECTED disabled-tool call name=%s (DEV_CONSOLE_FULL_CLI not set)",
            _AUDIT_MARKER,
            tool_name,
        )
        return json.dumps(
            {"error": f"Tool '{tool_name}' is not available (full-CLI disabled)."},
            ensure_ascii=False,
        )

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
        elif tool_name == "bash":
            result = bash(
                command=args.get("command", ""),
                timeout=int(args.get("timeout", _DEFAULT_BASH_TIMEOUT) or _DEFAULT_BASH_TIMEOUT),
            )
        elif tool_name == "read_file":
            result = read_file(path=args["path"])
        elif tool_name == "write_file":
            result = write_file(path=args["path"], content=args.get("content", ""))
        elif tool_name == "edit_file":
            result = edit_file(
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
