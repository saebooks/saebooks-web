"""routes/dev.py — live GUI dev console for saebooks-web.

  GET  /dev               — renders the self-contained dark dev console
  POST /dev/agent/chat    — SSE agent loop (mode='dev'); the agent edits the
                            app's own templates/ + static/ via the gui_* tools
                            and the preview iframe hot-reloads after each write.

Both endpoints are gated to the OWNER/ADMIN user only, reusing the exact gate
the admin SQL tool uses (saebooks_web/routes/admin.py): a valid session token
(``api_token``) AND the ``is_sae_staff`` session flag, which the login handler
sets from the ``SAE_STAFF_USERNAMES`` allowlist. On the prod instance that
allowlist is ``richard``. A non-staff session gets 403; an unauthenticated
request is redirected to /login.

This is the ONLY LLM-calling surface in saebooks-web. It talks to the bosun
LiteLLM gateway via saebooks_web.agent.client. The Claude subscription proxy
(claude-*-sub) does NOT support stream=True, so we call non-streaming per round
and chunk the reply to the client for a typing feel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from saebooks_web.agent import client as agent_client
from saebooks_web.agent import tools as agent_tools

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev", tags=["dev"])

# Templates live at the repo root (parent of the saebooks_web package). This
# module is saebooks_web/routes/dev.py, so walk up two parents to the package
# parent (the repo root): dev.py -> routes/ -> saebooks_web/ -> <repo root>.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Owner/admin gate — identical to saebooks_web/routes/admin.py (SQL tool).
# ---------------------------------------------------------------------------

def _require_auth(request: Request) -> str | None:
    """Return the session token if present, else None (caller redirects)."""
    return request.session.get("api_token")


def _is_sae_staff(request: Request) -> bool:
    """True if the session was flagged as SAE staff at login.

    Set by the login handler when the authenticated user matches the
    ``SAE_STAFF_USERNAMES`` allowlist. The dev console edits app source on
    disk, so it must be reachable by the owner only — never tenant admins.
    """
    return bool(request.session.get("is_sae_staff"))


# ---------------------------------------------------------------------------
# System prompt — single 'dev' mode describing saebooks-web's layout.
# ---------------------------------------------------------------------------

_DEV_SYSTEM_PROMPT = (
    "You are a live GUI developer embedded in THIS web app — SAE Books Web "
    "(books.sauer.com.au), a FastAPI + Jinja2 + HTMX server-rendered frontend. "
    "Richard is in a dev console asking you to change the app's interface, and "
    "you can edit the GUI source files directly and see them update live.\n"
    "Tools: gui_list_files() to discover files; gui_read_file(path) to read one "
    "(ALWAYS read before editing); gui_replace(path, find, replace) for small "
    "targeted edits (PREFERRED — match exactly incl. whitespace); "
    "gui_write_file(path, content) for new files or large rewrites.\n"
    "Layout: HTML lives in templates/ (Jinja2). 'templates/base.html' is the shell "
    "every full page extends (it carries the nav, the Tailwind stylesheet link "
    "<link rel=stylesheet href=/static/tailwind.css>, theme toggle, and the "
    "{% block content %}). Pages are 'templates/<section>/...' (e.g. "
    "'templates/dashboard.html', 'templates/expenses/list.html'); shared macros "
    "are the top-level 'templates/_*.html' files and 'templates/_components/'. "
    "Styling is Tailwind utility classes (compiled to 'static/tailwind.css' at "
    "build time — you cannot recompile Tailwind from here, so for new styling "
    "prefer existing utility classes already in the build, or add scoped inline "
    "<style> in a template). JS lives in 'static/js/'.\n"
    "You may ONLY edit under templates/ and static/ (backend .py is out of scope "
    "and would not hot-reload anyway). Jinja2 auto-reloads templates and "
    "StaticFiles serves from disk, so edits take effect LIVE on the next render — "
    "the dev console reloads its preview after each write.\n"
    "Work in small steps: read the relevant file, make the requested change, keep "
    "the markup valid Jinja2/HTML so the app keeps working, then say briefly what "
    "you changed and in which file. If a request is slightly ambiguous, make a "
    "sensible choice and say what you did — Richard wants velocity, not twenty "
    "questions."
)

# How many tool-calling rounds before we force a final text answer.
_MAX_TOOL_ROUNDS = 25


# ---------------------------------------------------------------------------
# GET /dev — render the console
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse, response_model=None)
@router.get("/", response_class=HTMLResponse, response_model=None)
async def dev_console(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the live GUI dev console (owner/admin only)."""
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_sae_staff(request):
        return HTMLResponse("Forbidden — SAE staff only", status_code=403)
    return _TEMPLATES.TemplateResponse(request, "dev/index.html", {})


# ---------------------------------------------------------------------------
# POST /dev/agent/chat — SSE agent loop
# ---------------------------------------------------------------------------

@router.post("/agent/chat", response_model=None)
async def dev_agent_chat(request: Request) -> StreamingResponse | RedirectResponse | HTMLResponse:
    """Accept { "message": str, "mode": "dev" } and stream back SSE.

    SSE line format (each line is ``data: <json>\\n\\n``):
      {"type": "token",       "text": "..."}   — partial assistant text
      {"type": "tool_call",   "name": "..."}   — tool being invoked (UI feedback)
      {"type": "gui_changed", "path": "..."}   — a GUI file was written (reload preview)
      {"type": "done"}                          — stream complete
      {"type": "error",       "text": "..."}   — something went wrong
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    if not _is_sae_staff(request):
        return HTMLResponse("Forbidden — SAE staff only", status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    message: str = (body.get("message") or "").strip()
    mode: str = (body.get("mode") or "dev").lower().strip()

    if not message:
        async def _empty() -> AsyncGenerator[str, None]:
            yield _sse({"type": "error", "text": "Empty message."})
            yield _sse({"type": "done"})
        return StreamingResponse(_empty(), media_type="text/event-stream")

    return StreamingResponse(
        _run_agent(message, mode),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Agent loop (generator)
# ---------------------------------------------------------------------------

async def _run_agent(user_message: str, mode: str) -> AsyncGenerator[str, None]:
    """Run the agent tool-calling loop and yield SSE strings.

    saebooks-web has no chat store (it is a thin REST client of saebooks-api),
    so this is single-turn: system prompt + the current user message. Tool
    results are appended in-loop; nothing is persisted.
    """
    model = agent_client.model_for_mode(mode)
    oai = agent_client.get_client()

    messages: list[dict] = [
        {"role": "system", "content": _DEV_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    full_assistant_text = ""

    try:
        for _round in range(_MAX_TOOL_ROUNDS + 1):
            # On the final round, force a text reply (no further tool calls).
            is_final_round = (_round == _MAX_TOOL_ROUNDS)
            forced_tool_choice = "none" if is_final_round else "auto"

            # NOTE: the Claude subscription proxy (claude-*-sub) does NOT support
            # stream=True — it returns empty deltas. So we call non-streaming and
            # chunk the reply to the client below to preserve the typing feel.
            completion = await oai.chat.completions.create(
                model=model,
                messages=messages,
                tools=agent_tools.TOOL_DEFINITIONS,
                tool_choice=forced_tool_choice,
                stream=False,
                max_tokens=4000,
            )

            choice = completion.choices[0] if completion.choices else None
            msg = choice.message if choice else None

            text_buf = (msg.content if msg and msg.content else "") or ""
            tool_calls_buf: dict[int, dict] = {}
            if msg and msg.tool_calls:
                for i, tc in enumerate(msg.tool_calls):
                    fn = tc.function
                    tool_calls_buf[i] = {
                        "id": tc.id or f"call_{i}",
                        "name": (fn.name if fn else "") or "",
                        "arguments": (fn.arguments if fn else "") or "",
                    }

            # Emit assistant text in small chunks (typing feel)
            if text_buf:
                full_assistant_text += text_buf
                for piece in _chunks(text_buf):
                    yield _sse({"type": "token", "text": piece})
                    await asyncio.sleep(0)

            # No tool calls (or final round forced text) -> done
            if not tool_calls_buf:
                if is_final_round and not text_buf:
                    logger.warning(
                        "Dev agent hit max_tool_rounds (%d) with no final text", _MAX_TOOL_ROUNDS
                    )
                    yield _sse({"type": "error", "text": "Agent reached the tool-call limit without a final answer."})
                break

            # --- Execute tool calls ---
            tool_calls_for_msg = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls_buf.values()
            ]
            messages.append(
                {"role": "assistant", "content": text_buf or None, "tool_calls": tool_calls_for_msg}
            )

            for tc in tool_calls_buf.values():
                tool_name = tc["name"]
                yield _sse({"type": "tool_call", "name": tool_name})
                # gui_* tools are synchronous (plain disk IO).
                result_json = agent_tools.dispatch(tool_name, tc["arguments"])
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": result_json}
                )
                # For GUI write tools: emit gui_changed so the console hot-reloads
                # its live-preview iframe.
                if tool_name in agent_tools.GUI_WRITE_TOOLS:
                    try:
                        gui_result = json.loads(result_json)
                    except Exception:
                        gui_result = {}
                    if not gui_result.get("error"):
                        yield _sse(
                            {"type": "gui_changed", "path": gui_result.get("path"), "tool": tool_name}
                        )

    except Exception as exc:
        logger.error("Dev agent stream error: %s", exc, exc_info=True)
        yield _sse({"type": "error", "text": f"Assistant error: {exc}"})
    finally:
        yield _sse({"type": "done"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    """Format a dict as a ``data: <json>\\n\\n`` SSE line."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunks(text: str, size: int = 4) -> Iterator[str]:
    """Split text into small word-group chunks (preserving spacing) for a
    progressive typing feel, since the upstream proxy can't truly stream."""
    parts = re.findall(r"\S+\s*", text)
    if not parts:
        yield text
        return
    for i in range(0, len(parts), size):
        yield "".join(parts[i:i + size])
