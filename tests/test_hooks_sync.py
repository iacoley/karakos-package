"""Tests for bin/hooks-sync.py."""
import json
import shutil

import pytest

from conftest import PACKAGE_ROOT, import_script

sync_mod = import_script("hooks-sync")


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(PACKAGE_ROOT / "config" / "claude-settings.json", tmp_path / "config")
    return tmp_path


def commands(settings, event="PreToolUse"):
    return [h["command"] for g in settings["hooks"].get(event, []) for h in g["hooks"]]


def load(ws):
    return json.loads((ws / "config" / "claude-settings.json").read_text())


def test_defaults_created_and_rails_wired(ws):
    assert not (ws / "config" / "hooks.json").exists()
    sync_mod.sync(ws)
    cfg = json.loads((ws / "config" / "hooks.json").read_text())
    assert cfg == {"heavy_build_block": False, "bare_ssh_hosts": [], "ssh_wrapper": ""}
    cmds = commands(load(ws))
    assert any(c.endswith("bash-safety-rails.py") for c in cmds)
    assert any(c.endswith("block-bare-ssh.py") for c in cmds)
    assert sum(c.endswith("protected-paths-edit.py") for c in cmds) == 1  # one group: file tools + Bash
    assert not any("block-heavy-build" in c for c in cmds)


def test_sleep_poll_is_gone(ws):
    s = load(ws)
    s["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
        {"type": "command", "command": "$WORKSPACE_ROOT/system/hooks/rewrite-sleep-poll.py"}]})
    (ws / "config" / "claude-settings.json").write_text(json.dumps(s))
    sync_mod.sync(ws)
    assert "sleep-poll" not in (ws / "config" / "claude-settings.json").read_text()


def test_shipped_settings_match_sync_output(ws):
    shipped = (ws / "config" / "claude-settings.json").read_text()
    assert sync_mod.sync(ws) is False
    assert (ws / "config" / "claude-settings.json").read_text() == shipped
    assert "sleep-poll" not in shipped


def test_heavy_build_wired_only_with_flag(ws):
    (ws / "config" / "hooks.json").write_text(json.dumps({"heavy_build_block": True}))
    sync_mod.sync(ws)
    assert any(c.endswith("block-heavy-build.py") for c in commands(load(ws)))
    (ws / "config" / "hooks.json").write_text(json.dumps({"heavy_build_block": False}))
    sync_mod.sync(ws)
    assert not any("block-heavy-build" in c for c in commands(load(ws)))


def test_idempotent(ws):
    sync_mod.sync(ws)
    first = (ws / "config" / "claude-settings.json").read_text()
    assert sync_mod.sync(ws) is False
    assert (ws / "config" / "claude-settings.json").read_text() == first


def test_preserves_unmanaged_hooks_permissions_env(ws):
    s = load(ws)
    s["permissions"] = {"allow": ["Bash(ls:*)"], "deny": ["Read(.env)"]}
    s["env"] = {"FOO": "bar"}
    s["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/opt/mine/audit.sh"}]})
    s["hooks"]["PostToolUse"] = [{"hooks": [{"type": "command", "command": "/opt/mine/post.sh"}]}]
    (ws / "config" / "claude-settings.json").write_text(json.dumps(s))
    sync_mod.sync(ws)
    out = load(ws)
    assert out["permissions"] == s["permissions"] and out["env"] == s["env"]
    assert "/opt/mine/audit.sh" in commands(out)
    assert commands(out, "PostToolUse") == ["/opt/mine/post.sh"]
    before = (ws / "config" / "claude-settings.json").read_text()
    assert sync_mod.sync(ws) is False
    assert (ws / "config" / "claude-settings.json").read_text() == before


def test_unmanaged_hook_mixed_into_managed_group_survives(ws):
    s = load(ws)
    bash_group = [g for g in s["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"][0]
    bash_group["hooks"].append({"type": "command", "command": "/opt/mine/x.sh"})
    (ws / "config" / "claude-settings.json").write_text(json.dumps(s))
    sync_mod.sync(ws)
    assert "/opt/mine/x.sh" in commands(load(ws))
    sync_mod.sync(ws)
    assert commands(load(ws)).count("/opt/mine/x.sh") == 1


def test_all_wired_hook_files_exist():
    for c in commands(json.loads((PACKAGE_ROOT / "config" / "claude-settings.json").read_text()), "PreToolUse"):
        assert (PACKAGE_ROOT / c.replace("$WORKSPACE_ROOT/", "")).exists(), c


def test_merged_settings_have_exactly_one_inject_recall(ws):
    sync_mod.sync(ws)
    sync_mod.sync(ws)  # a second sync must not duplicate it
    cmds = commands(load(ws), "UserPromptSubmit")
    assert sum("inject-recall.py" in c for c in cmds) == 1
