"""
Tests for issue #96 — PreToolUse hook rewriting a blocked sleep-poll Bash
command into the sanctioned wait-for.sh (#93).

Each test invokes the real hook script via subprocess with the exact JSON
Claude Code would send on stdin, and asserts on the real stdout it produces.
Removing or breaking system/hooks/rewrite-sleep-poll.py fails every test
here with a nonzero/FileNotFoundError, not a mock mismatch.
"""

import json
import subprocess
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent
HOOK = PACKAGE_ROOT / "system" / "hooks" / "rewrite-sleep-poll.py"
SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"


def run_hook(payload):
    result = subprocess.run(
        ["python3", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_hook_script_exists_and_is_executable():
    import stat
    assert HOOK.exists()
    assert HOOK.stat().st_mode & stat.S_IXUSR


def test_settings_file_wires_rewrite_sleep_poll_on_bash():
    config = json.loads(SETTINGS_PATH.read_text())
    entries = config["hooks"]["PreToolUse"]
    bash_entries = [e for e in entries if e.get("matcher") == "Bash"]
    assert bash_entries, "no PreToolUse entry matches the Bash tool"
    commands = [h["command"] for e in bash_entries for h in e["hooks"]]
    assert any("rewrite-sleep-poll.py" in c for c in commands)


def test_rewrites_sleep_and_command_with_semicolon():
    out = run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 300; gh pr checks 674"},
    })
    payload = json.loads(out)
    new_cmd = payload["hookSpecificOutput"]["updatedInput"]["command"]
    assert "wait-for.sh --sleep 300" in new_cmd
    assert "gh pr checks 674" in new_cmd
    assert new_cmd.strip().startswith("sleep") is False


def test_rewrites_sleep_and_command_with_double_ampersand():
    out = run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 60 && tail -20 /tmp/harvest.log"},
    })
    payload = json.loads(out)
    new_cmd = payload["hookSpecificOutput"]["updatedInput"]["command"]
    assert "wait-for.sh --sleep 60" in new_cmd
    assert "&&" in new_cmd
    assert "tail -20 /tmp/harvest.log" in new_cmd


def test_preserves_other_tool_input_fields():
    out = run_hook({
        "tool_name": "Bash",
        "tool_input": {
            "command": "sleep 5; echo done",
            "description": "wait then report",
        },
    })
    payload = json.loads(out)
    updated = payload["hookSpecificOutput"]["updatedInput"]
    assert updated["description"] == "wait then report"


def test_bare_sleep_with_nothing_after_is_left_alone():
    assert run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 300"},
    }) == ""


def test_sleep_not_at_start_of_command_is_left_alone():
    assert run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi; sleep 300; echo bye"},
    }) == ""


def test_non_bash_tool_is_ignored():
    assert run_hook({
        "tool_name": "Edit",
        "tool_input": {"command": "sleep 5; echo hi"},
    }) == ""


def test_non_sleep_command_is_ignored():
    assert run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    }) == ""


def test_malformed_json_stdin_does_not_crash():
    result = subprocess.run(
        ["python3", str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == ""
