"""
Tests for issue #94 — hooks wiring via --settings on the claude spawn line.

PR #114 review (2026-08-06) flagged the original two acceptance tests here:
  - test_agent_server_passes_settings_flag_on_spawn was a source-text grep
    on agent-server.py — passes even if --settings is in a comment, breaks
    on any cosmetic refactor.
  - test_hook_script_appends_one_line_per_invocation ran
    system/hooks/log-user-prompt.sh directly with bash. It never touches
    bin/agent-server.py, so it passed unchanged with the entire #94 fix
    reverted.

Replacements below actually exercise the dispatch path:
  - test_agent_server_wires_settings_flag_into_real_spawn_argv calls the
    real start_agent_subprocess() with subprocess creation patched, and
    asserts on the literal argv it builds.
  - test_real_claude_dispatch_fires_user_prompt_submit_hook spawns the real
    `claude` CLI with the package's shipped settings.json and asserts the
    hook fires and appends exactly one line — the issue's actual
    acceptance criterion, no mocks. Marked slow (needs the `claude` binary
    and live credentials, absent on the GitHub Actions runner that CI's
    unit-tests job runs on) so `-m "not slow"` keeps CI green; run directly
    on hardware that has both, per the review.
"""

import asyncio
import json
import os
import shutil
import stat
import subprocess

import pytest

from conftest import PACKAGE_ROOT, import_script

AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"
HOOK_SCRIPT = PACKAGE_ROOT / "system" / "hooks" / "log-user-prompt.sh"
CLAUDE_BIN = shutil.which("claude")


def test_settings_file_exists_and_is_package_owned():
    """The settings file must ship in config/, not require the installer or
    the user to scaffold a .claude/ dir."""
    assert SETTINGS_PATH.exists()
    assert not (PACKAGE_ROOT / ".claude").exists()


def test_settings_file_wires_user_prompt_submit_hook():
    config = json.loads(SETTINGS_PATH.read_text())
    hooks = config.get("hooks", {})
    assert "UserPromptSubmit" in hooks
    entries = hooks["UserPromptSubmit"]
    assert entries, "UserPromptSubmit hook list is empty"
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert any("log-user-prompt.sh" in cmd for cmd in commands)


def test_agent_server_wires_settings_flag_into_real_spawn_argv(tmp_workspace, monkeypatch):
    """Calls the real start_agent_subprocess() — not a source-text grep —
    with asyncio.create_subprocess_exec patched to capture argv instead of
    actually spawning. Fails if the --settings flag is dropped from the
    spawn line, unlike the grep it replaces (which passed even with
    --settings sitting in a comment)."""
    settings_path = tmp_workspace / "config" / "claude-settings.json"
    settings_path.write_text(json.dumps({"hooks": {}}))

    agent_dir = tmp_workspace / "agents" / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "SYSTEM_PROMPT.md").write_text("You are a test agent.")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    captured = {}

    class FakeStderr:
        async def readline(self):
            return b""

    class FakeProc:
        pid = 4242
        stderr = FakeStderr()

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return FakeProc()

    monkeypatch.setattr(
        agent_server.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    async def run():
        await agent_server.init_db()
        await agent_server.load_config()
        await agent_server.start_agent_subprocess("test-agent")
        await asyncio.sleep(0)  # let the stderr_reader task drain and exit

    asyncio.run(run())

    assert "cmd" in captured, "start_agent_subprocess never reached create_subprocess_exec"
    cmd = captured["cmd"]
    assert "--settings" in cmd, f"--settings missing from spawn argv: {cmd}"
    idx = cmd.index("--settings")
    assert cmd[idx + 1] == str(settings_path), cmd


def test_hook_script_is_executable():
    assert HOOK_SCRIPT.exists()
    mode = HOOK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "hook script must be executable for claude to invoke it"


@pytest.mark.slow
@pytest.mark.skipif(CLAUDE_BIN is None, reason="claude CLI not installed on this box")
def test_real_claude_dispatch_fires_user_prompt_submit_hook(tmp_workspace):
    """The issue's actual acceptance test: a live `claude` process, given
    the package's shipped settings.json, fires the UserPromptSubmit hook
    and appends exactly one line — through the real CLI, not by invoking
    system/hooks/log-user-prompt.sh directly with bash (which the old
    version of this test did, and which passed even with the entire #94
    fix reverted since it never goes near --settings or claude itself).

    WORKSPACE_ROOT points at tmp_workspace so both halves of the shipped
    settings.json's "$WORKSPACE_ROOT/system/hooks/log-user-prompt.sh"
    command resolve there: the script (copied in below, matching how a
    real install has system/hooks/ inside WORKSPACE_ROOT) and the log
    output. --settings itself points at the actual repo-shipped
    config/claude-settings.json, unmodified.
    """
    hooks_dir = tmp_workspace / "system" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    copied_script = hooks_dir / "log-user-prompt.sh"
    shutil.copy(HOOK_SCRIPT, copied_script)
    copied_script.chmod(copied_script.stat().st_mode | stat.S_IXUSR)

    log_path = tmp_workspace / "logs" / "hook-events.log"
    assert not log_path.exists()

    env = dict(os.environ, WORKSPACE_ROOT=str(tmp_workspace))
    result = subprocess.run(
        [CLAUDE_BIN, "-p", "Reply with the single word: hi",
         "--settings", str(SETTINGS_PATH)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"claude -p exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    assert log_path.exists(), "UserPromptSubmit hook never fired: hook-events.log was never created"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1, f"expected exactly one hook-fired line, got: {lines!r}"
