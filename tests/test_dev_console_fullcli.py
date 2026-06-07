"""Tests for the dev-console full-CLI tools + the DEV_CONSOLE_FULL_CLI gate.

The /dev console (saebooks_web.routes.dev) ships GUI-editor tools (gui_*) always,
and an extra full Claude-CLI surface (bash / read_file / write_file / edit_file)
ONLY when the DEV_CONSOLE_FULL_CLI env flag is truthy. Prod runs with the flag
unset, so the full-CLI tools must be neither advertised nor dispatchable there.

Coverage:
  Flag SET (full-CLI on):
    - tool_definitions()/available_tool_names() include the full-CLI tools
    - bash runs a real command (echo hi) and captures stdout + exit code
    - read_file reads a NON-template file (this test file / pyproject)
    - write_file writes OUTSIDE templates/ + static/ (a tmp path)
    - edit_file edits an arbitrary file
  Flag UNSET (prod default):
    - tool_definitions()/available_tool_names() expose ONLY gui_* tools
    - a direct dispatch of 'bash' is REJECTED (error JSON, command never runs)
    - the gui_* tools still work
  Both /dev endpoints still 403 without a staff session (gate UNCHANGED), in
  both flag states.
"""
from __future__ import annotations

import json as _json
from base64 import b64encode as _b64encode

import pytest
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner as _TimestampSigner

from saebooks_web.agent import tools as agent_tools
from saebooks_web.config import settings
from saebooks_web.main import app

_FLAG = "DEV_CONSOLE_FULL_CLI"
_FULL_CLI_NAMES = {"bash", "read_file", "write_file", "edit_file"}
_GUI_NAMES = {"gui_list_files", "gui_read_file", "gui_replace", "gui_write_file"}


# ---------------------------------------------------------------------------
# Session cookie helpers (mirrors test_admin_sql_tool_web.py)
# ---------------------------------------------------------------------------

def _make_session_cookie(data: dict) -> str:
    signer = _TimestampSigner(settings.secret_key)
    payload = _b64encode(_json.dumps(data).encode("utf-8"))
    return signer.sign(payload).decode("utf-8")


_NON_STAFF_COOKIE = _make_session_cookie(
    {"api_token": "t-nonstaff", "is_sae_staff": False, "user_role": "admin"}
)
_STAFF_COOKIE = _make_session_cookie(
    {"api_token": "t-staff", "is_sae_staff": True, "user_role": "admin"}
)


def _names(defs: list[dict]) -> set[str]:
    return {d["function"]["name"] for d in defs}


# ===========================================================================
# Flag SET — full-CLI tools registered + dispatchable
# ===========================================================================

def test_flag_set_registers_full_cli_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag set, tool schemas + available names include the full-CLI tools."""
    monkeypatch.setenv(_FLAG, "1")
    assert agent_tools.full_cli_enabled() is True

    schema_names = _names(agent_tools.tool_definitions())
    assert schema_names >= _FULL_CLI_NAMES, "full-CLI tools missing from schema"
    assert schema_names >= _GUI_NAMES, "gui_* tools must still be present"

    avail = agent_tools.available_tool_names()
    assert avail >= _FULL_CLI_NAMES
    assert avail >= _GUI_NAMES


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_flag_truthy_variants(monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
    """A range of truthy spellings all enable full-CLI."""
    monkeypatch.setenv(_FLAG, truthy)
    assert agent_tools.full_cli_enabled() is True


def test_bash_runs_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """bash('echo hi') returns stdout 'hi' and exit_code 0."""
    monkeypatch.setenv(_FLAG, "1")
    out = _json.loads(agent_tools.dispatch("bash", _json.dumps({"command": "echo hi"})))
    assert out.get("exit_code") == 0
    assert out.get("stdout", "").strip() == "hi"
    assert "error" not in out


def test_bash_captures_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """bash captures stderr + a non-zero exit code without raising."""
    monkeypatch.setenv(_FLAG, "1")
    out = _json.loads(
        agent_tools.dispatch("bash", _json.dumps({"command": "echo oops 1>&2; exit 3"}))
    )
    assert out.get("exit_code") == 3
    assert "oops" in out.get("stderr", "")


def test_read_file_reads_non_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_file can read a NON-template file (pyproject.toml at repo root)."""
    monkeypatch.setenv(_FLAG, "1")
    out = _json.loads(agent_tools.dispatch("read_file", _json.dumps({"path": "pyproject.toml"})))
    assert "error" not in out
    assert "saebooks-web" in out.get("content", "")
    # Crucially this is a path gui_read_file would reject.
    gui = _json.loads(
        agent_tools.dispatch("gui_read_file", _json.dumps({"path": "pyproject.toml"}))
    )
    assert "error" in gui


def test_write_file_outside_gui_roots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """write_file writes OUTSIDE templates/ + static/ (an absolute tmp path)."""
    monkeypatch.setenv(_FLAG, "1")
    target = tmp_path / "sub" / "note.txt"
    out = _json.loads(
        agent_tools.dispatch(
            "write_file", _json.dumps({"path": str(target), "content": "hello fullcli"})
        )
    )
    assert "error" not in out
    assert target.read_text() == "hello fullcli"
    assert out.get("action") == "created"


def test_edit_file_arbitrary_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """edit_file does an exact-substring replace on an arbitrary file."""
    monkeypatch.setenv(_FLAG, "1")
    target = tmp_path / "f.txt"
    target.write_text("alpha beta gamma")
    out = _json.loads(
        agent_tools.dispatch(
            "edit_file",
            _json.dumps({"path": str(target), "find": "beta", "replace": "BETA"}),
        )
    )
    assert "error" not in out
    assert out.get("replaced") == 1
    assert target.read_text() == "alpha BETA gamma"


def test_mutating_set_includes_full_cli() -> None:
    """The mutating-tools set includes the write/exec full-CLI tools."""
    assert {"bash", "write_file", "edit_file"} <= agent_tools.MUTATING_TOOLS
    # read_file is non-mutating; gui writes are still mutating.
    assert "read_file" not in agent_tools.MUTATING_TOOLS
    assert {"gui_write_file", "gui_replace"} <= agent_tools.MUTATING_TOOLS


# ===========================================================================
# Flag UNSET — full-CLI tools NOT registered + dispatch rejected
# ===========================================================================

def test_flag_unset_hides_full_cli_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag unset, ONLY gui_* tools are advertised/available."""
    monkeypatch.delenv(_FLAG, raising=False)
    assert agent_tools.full_cli_enabled() is False

    schema_names = _names(agent_tools.tool_definitions())
    assert schema_names == _GUI_NAMES, f"unexpected tools when flag off: {schema_names}"
    assert not (_FULL_CLI_NAMES & schema_names)

    avail = agent_tools.available_tool_names()
    assert avail == frozenset(_GUI_NAMES)


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "nope"])
def test_flag_falsy_variants(monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
    """Falsy / unrecognised values keep full-CLI disabled."""
    monkeypatch.setenv(_FLAG, falsy)
    assert agent_tools.full_cli_enabled() is False


def test_bash_dispatch_rejected_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct dispatch of 'bash' is rejected (error JSON) when the flag is off."""
    monkeypatch.delenv(_FLAG, raising=False)
    out = _json.loads(
        agent_tools.dispatch("bash", _json.dumps({"command": "echo SHOULD_NOT_RUN"}))
    )
    assert "error" in out
    assert "exit_code" not in out
    assert "SHOULD_NOT_RUN" not in _json.dumps(out)


@pytest.mark.parametrize("tool", sorted(_FULL_CLI_NAMES))
def test_all_full_cli_tools_rejected_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """Every full-CLI tool is rejected at dispatch when the flag is off."""
    monkeypatch.delenv(_FLAG, raising=False)
    out = _json.loads(
        agent_tools.dispatch(tool, _json.dumps({"path": "pyproject.toml", "command": "echo x"}))
    )
    assert "error" in out


def test_gui_tools_work_with_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """gui_* tools still function normally when the flag is off."""
    monkeypatch.delenv(_FLAG, raising=False)
    out = _json.loads(agent_tools.dispatch("gui_list_files", _json.dumps({"subdir": "templates"})))
    assert "error" not in out
    assert out.get("count", 0) >= 1


# ===========================================================================
# Owner gate UNCHANGED — both /dev endpoints 403 for a non-staff session,
# regardless of the flag.
# ===========================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("flag_on", [True, False])
async def test_dev_endpoints_forbidden_for_non_staff(
    monkeypatch: pytest.MonkeyPatch, flag_on: bool
) -> None:
    """GET /dev and POST /dev/agent/chat must 403 a logged-in non-staff user."""
    if flag_on:
        monkeypatch.setenv(_FLAG, "1")
    else:
        monkeypatch.delenv(_FLAG, raising=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _NON_STAFF_COOKIE},
        follow_redirects=False,
    ) as client:
        get_resp = await client.get("/dev")
        post_resp = await client.post("/dev/agent/chat", json={"message": "hi", "mode": "dev"})

    assert get_resp.status_code == 403
    assert post_resp.status_code == 403


@pytest.mark.anyio
async def test_dev_page_renders_full_cli_badge_for_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag on, a staff session sees the full-CLI console label."""
    monkeypatch.setenv(_FLAG, "1")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _STAFF_COOKIE},
    ) as client:
        resp = await client.get("/dev")
    assert resp.status_code == 200
    assert "full CLI" in resp.text
    assert "bash + arbitrary file access" in resp.text


@pytest.mark.anyio
async def test_dev_page_renders_gui_label_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag off, a staff session sees the GUI-only console label."""
    monkeypatch.delenv(_FLAG, raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: _STAFF_COOKIE},
    ) as client:
        resp = await client.get("/dev")
    assert resp.status_code == 200
    assert "GUI Dev Console" in resp.text
    assert "bash + arbitrary file access" not in resp.text


@pytest.mark.anyio
async def test_dev_endpoints_redirect_when_unauthenticated() -> None:
    """No session at all -> 303 redirect to /login (gate unchanged)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        get_resp = await client.get("/dev")
        post_resp = await client.post("/dev/agent/chat", json={"message": "hi", "mode": "dev"})

    assert get_resp.status_code == 303
    assert get_resp.headers["location"] == "/login"
    assert post_resp.status_code == 303
