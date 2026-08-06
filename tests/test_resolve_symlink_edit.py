"""
Tests for issue #95 — PreToolUse hook resolving Edit/Write/Read through a
symlink in flight, so the model never sees Claude Code's "refusing to write
through symlink" rejection.

Each test invokes the real hook script via subprocess with the exact JSON
Claude Code would send on stdin, and asserts on the real stdout it
produces. Removing or breaking system/hooks/resolve-symlink-edit.py fails
every test here with a nonzero/FileNotFoundError, not a mock mismatch.
"""

import json
import os
import subprocess
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent
HOOK = PACKAGE_ROOT / "system" / "hooks" / "resolve-symlink-edit.py"
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


def test_settings_file_wires_resolve_symlink_edit():
    config = json.loads(SETTINGS_PATH.read_text())
    entries = config["hooks"]["PreToolUse"]
    matchers = [e.get("matcher", "") for e in entries]
    hit = [e for e in entries if "Edit" in e.get("matcher", "")]
    assert hit, f"no PreToolUse entry matches Edit, got matchers={matchers}"
    commands = [h["command"] for e in hit for h in e["hooks"]]
    assert any("resolve-symlink-edit.py" in c for c in commands)


def test_rewrites_edit_through_a_symlinked_file(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hello")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    out = run_hook({
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(link),
            "old_string": "hello",
            "new_string": "goodbye",
        },
    })
    payload = json.loads(out)
    updated = payload["hookSpecificOutput"]["updatedInput"]
    assert updated["file_path"] == str(real.resolve())
    assert updated["old_string"] == "hello"


def test_rewrites_through_a_symlinked_parent_directory(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "file.txt").write_text("x")
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir)

    out = run_hook({
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(link_dir / "file.txt"),
            "content": "new content",
        },
    })
    payload = json.loads(out)
    updated = payload["hookSpecificOutput"]["updatedInput"]
    assert updated["file_path"] == str((real_dir / "file.txt").resolve())


def test_read_is_also_rewritten_to_keep_read_state_in_sync(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hello")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    out = run_hook({
        "tool_name": "Read",
        "tool_input": {"file_path": str(link)},
    })
    payload = json.loads(out)
    updated = payload["hookSpecificOutput"]["updatedInput"]
    assert updated["file_path"] == str(real.resolve())


def test_non_symlink_path_is_left_alone(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hello")

    assert run_hook({
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(real),
            "old_string": "hello",
            "new_string": "goodbye",
        },
    }) == ""


def test_nonexistent_path_is_left_alone(tmp_path):
    missing = tmp_path / "does" / "not" / "exist.txt"
    assert run_hook({
        "tool_name": "Write",
        "tool_input": {"file_path": str(missing), "content": "x"},
    }) == ""


def test_untargeted_tool_is_ignored():
    assert run_hook({
        "tool_name": "Bash",
        "tool_input": {"file_path": "/tmp/some-symlink"},
    }) == ""


def test_missing_file_path_field_does_not_crash():
    assert run_hook({
        "tool_name": "Edit",
        "tool_input": {"old_string": "a", "new_string": "b"},
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
