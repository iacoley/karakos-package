"""Tests for system/hooks/protected-paths-edit.py (G9 build A).

Each test runs the real hook through subprocess with a real PreToolUse JSON
payload against a temp root that carries a copy of config/protected-paths.json.
"""
import json
import os
import stat
import subprocess
import shutil
import sys

import pytest

from conftest import PACKAGE_ROOT, import_script

HOOK = PACKAGE_ROOT / "system" / "hooks" / "protected-paths-edit.py"
REASON = "Protected path (tier 1): {}. Do not edit it; report to the owner."


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(PACKAGE_ROOT / "config" / "protected-paths.json", tmp_path / "config")
    return tmp_path


def run(root, payload, agent="marvin", raw=None):
    env = {k: v for k, v in os.environ.items() if k not in ("KARAKOS_AGENT", "WORKSPACE_ROOT")}
    env["WORKSPACE_ROOT"] = str(root)
    if agent is not None:
        env["KARAKOS_AGENT"] = agent
    res = subprocess.run([sys.executable, str(HOOK)],
                         input=raw if raw is not None else json.dumps(payload),
                         capture_output=True, text=True, timeout=10, env=env)
    assert res.returncode == 0, res.stderr
    return res


def edit(path, tool="Edit"):
    return {"tool_name": tool, "tool_input": {"file_path": str(path)}}


def bash(cmd, cwd=None):
    p = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd:
        p["cwd"] = str(cwd)
    return p


def decision(res):
    return json.loads(res.stdout)["hookSpecificOutput"]


def log_lines(root):
    f = root / "logs" / "protected-edits.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_tier1_edit_denied_with_exact_reason_and_logged(root):
    out = decision(run(root, edit(root / "bin" / "relay.py")))
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == REASON.format("bin/relay.py")
    (rec,) = log_lines(root)
    assert set(rec) == {"ts", "agent", "tool", "path", "tier", "decision"}
    assert (rec["agent"], rec["tool"], rec["path"], rec["tier"], rec["decision"]) == \
        ("marvin", "Edit", "bin/relay.py", 1, "deny")


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("MultiEdit", "file_path"),
                                       ("NotebookEdit", "notebook_path")])
def test_other_edit_tools_denied(root, tool, key):
    res = run(root, {"tool_name": tool, "tool_input": {key: str(root / "system" / "x.py")}})
    assert decision(res)["permissionDecision"] == "deny"


def test_no_agent_env_does_nothing(root):
    res = run(root, edit(root / "bin" / "relay.py"), agent=None)
    assert res.stdout == ""
    assert log_lines(root) == []
    assert run(root, edit(root / "bin" / "relay.py"), agent="").stdout == ""


def test_override_allowed_without_log(root):
    res = run(root, edit(root / "agents" / "marvin" / "persona" / "notes.md"))
    assert res.stdout == "" and log_lines(root) == []


def test_tier2_allowed_and_logged(root):
    res = run(root, edit(root / "bin" / "friction-sensor.py"))
    assert res.stdout == ""
    (rec,) = log_lines(root)
    assert rec["tier"] == 2 and rec["decision"] == "allow"
    assert rec["path"] == "bin/friction-sensor.py" and rec["agent"] == "marvin"
    assert set(rec) == {"ts", "agent", "tool", "path", "tier", "decision"}


def test_unprotected_path_allowed(root):
    assert run(root, edit(root / "notes" / "a.md")).stdout == ""
    assert log_lines(root) == []


def test_outside_root_allowed(root, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    res = run(root, edit(other / "bin" / "relay.py"))
    assert res.stdout == "" and log_lines(root) == []


def test_bash_sed_in_place_denied(root):
    res = run(root, bash("sed -i s/a/b/ bin/relay.py", cwd=root))
    assert decision(res)["permissionDecisionReason"] == REASON.format("bin/relay.py")


def test_bash_relative_path_defaults_to_root(root):
    assert decision(run(root, bash("rm bin/relay.py")))["permissionDecision"] == "deny"


def test_bash_cwd_changes_resolution(root):
    (root / "bin").mkdir()
    assert run(root, bash("rm relay.py", cwd=root / "notes")).stdout == ""
    assert decision(run(root, bash("rm relay.py", cwd=root / "bin")))["permissionDecision"] == "deny"


def test_bash_read_is_allowed(root):
    assert run(root, bash("cat bin/relay.py", cwd=root)).stdout == ""


def test_bash_cp_from_config_denied(root):
    res = run(root, bash("cp config/agents.yaml /tmp/x", cwd=root))
    assert decision(res)["permissionDecisionReason"] == REASON.format("config/agents.yaml")


def test_bash_dd_of_and_wrapped_and_chained(root):
    assert decision(run(root, bash("dd if=/dev/zero of=bin/relay.py")))["permissionDecision"] == "deny"
    assert decision(run(root, bash("echo hi && sudo tee system/hooks/x.py")))["permissionDecision"] == "deny"
    assert decision(run(root, bash("bash -c 'mv a.txt bin/scheduler.py'")))["permissionDecision"] == "deny"


def test_bash_tier2_logged_with_bash_tool(root):
    assert run(root, bash("cp /tmp/x bin/friction-sensor.py")).stdout == ""
    (rec,) = log_lines(root)
    assert rec["tool"] == "Bash" and rec["tier"] == 2


def test_bash_redirection_is_a_known_gap(root):
    assert run(root, bash("echo x > bin/relay.py", cwd=root)).stdout == ""


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"tool_name": "Edit"}',
                                 '{"tool_name": "Edit", "tool_input": {"file_path": 5}}',
                                 '{"tool_name": "Bash", "tool_input": {}}'])
def test_malformed_or_missing_fields_fail_open(root, raw):
    res = run(root, None, raw=raw)
    assert res.stdout == ""


def test_symlink_to_protected_file_denied(root):
    (root / "bin").mkdir()
    (root / "bin" / "relay.py").write_text("x")
    link = root / "notes-link.py"
    link.symlink_to(root / "bin" / "relay.py")
    res = run(root, edit(link))
    assert decision(res)["permissionDecisionReason"] == REASON.format("bin/relay.py")


def test_missing_config_allows_with_stderr_line(root):
    (root / "config" / "protected-paths.json").unlink()
    res = run(root, edit(root / "bin" / "relay.py"))
    assert res.stdout == ""
    assert len(res.stderr.strip().splitlines()) == 1


def test_hook_is_executable():
    assert HOOK.stat().st_mode & stat.S_IXUSR


def test_hooks_sync_wires_hook_once_for_file_tools_and_bash():
    desired = import_script("hooks-sync").desired({})["PreToolUse"]
    groups = [g for g in desired
              if any(h["command"].endswith("protected-paths-edit.py") for h in g["hooks"])]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash"


def test_shipped_settings_wire_hook():
    settings = json.loads((PACKAGE_ROOT / "config" / "claude-settings.json").read_text())
    groups = [g for g in settings["hooks"]["PreToolUse"]
              if any(h["command"].endswith("protected-paths-edit.py") for h in g["hooks"])]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash"
