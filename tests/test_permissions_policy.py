"""
Tests for issue #99 — permissions.allow/deny + per-agent env, replacing an
unconditional --dangerously-skip-permissions with reviewable config.

Empirically verified against the real `claude` CLI before writing any of
this (not asserted here, since it needs the live binary — the slow test at
the bottom automates the same check):

  - permissions.deny in --settings drops the named tool from the session's
    tool list at spawn (the "system"/"init" stream-json event's "tools"
    array), and --dangerously-skip-permissions does NOT put it back. A
    fine-grained rule like "Bash(curl:*)" behaves differently: the tool
    stays in the list, but a call matching the rule is declined at request
    time, and the CLI's "result" event carries a "permission_denials" list
    describing exactly what was denied.
  - settings.json's "env" key IS applied to the CLI's own subprocess
    environment (confirmed with `echo $VAR` through Bash), but it is one
    shared value for every agent since config/claude-settings.json is a
    single file all agents pass via --settings. Real per-agent env needs a
    per-agent source, which is what agents.json's new "env" key here feeds
    into agent-server.py's subprocess env.

Given that split, "declines and logs" is proven two ways rather than one
big integration test:
  - test_real_claude_dispatch_denies_webfetch_when_settings_deny_it (slow,
    real CLI): proves the decline — a denied tool is structurally absent
    from the session, so the model cannot call it.
  - test_read_agent_response_logs_permission_denials /
    test_read_agent_response_logs_system_init_tool_list (fast, no CLI
    needed): prove the log — agent-server.py's read_agent_response()
    actually writes both the resolved tool list and any runtime denial to
    the log, exercised against a real asyncio coroutine with a stubbed
    stdout stream, not a source-text grep.
"""

import asyncio
import json
import logging
import shutil
import subprocess

import pytest

from conftest import PACKAGE_ROOT, import_script

AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"
CLAUDE_BIN = shutil.which("claude")


# ---------------------------------------------------------------------------
# Static config shape
# ---------------------------------------------------------------------------

def test_settings_file_has_permissions_and_env_keys():
    config = json.loads(SETTINGS_PATH.read_text())
    assert "permissions" in config
    assert "allow" in config["permissions"]
    assert "deny" in config["permissions"]
    assert "env" in config
    # Shipped defaults are no-op — an install with no policy set behaves
    # exactly as before.
    assert config["permissions"]["allow"] == []
    assert config["permissions"]["deny"] == []
    assert config["env"] == {}


# ---------------------------------------------------------------------------
# load_permission_policy()
# ---------------------------------------------------------------------------

def test_load_permission_policy_reads_deny_list(monkeypatch, tmp_workspace):
    settings_path = tmp_workspace / "config" / "claude-settings.json"
    settings_path.write_text(json.dumps({
        "permissions": {"allow": ["Read"], "deny": ["WebFetch", "Bash(curl:*)"]},
    }))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    allow, deny = agent_server.load_permission_policy()
    assert allow == ["Read"]
    assert deny == ["WebFetch", "Bash(curl:*)"]


def test_load_permission_policy_missing_file_is_noop(monkeypatch, tmp_workspace):
    settings_path = tmp_workspace / "config" / "claude-settings.json"
    assert not settings_path.exists()
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    assert agent_server.load_permission_policy() == ([], [])


def test_load_permission_policy_malformed_json_is_noop_not_a_crash(monkeypatch, tmp_workspace):
    settings_path = tmp_workspace / "config" / "claude-settings.json"
    settings_path.write_text("{not valid json")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    assert agent_server.load_permission_policy() == ([], [])


# ---------------------------------------------------------------------------
# extract_permission_denials()
# ---------------------------------------------------------------------------

def test_extract_permission_denials_returns_list(monkeypatch, tmp_workspace):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    event = {"type": "result", "permission_denials": [{"tool_name": "Bash"}]}
    assert agent_server.extract_permission_denials(event) == [{"tool_name": "Bash"}]


def test_extract_permission_denials_defaults_to_empty_list(monkeypatch, tmp_workspace):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    assert agent_server.extract_permission_denials({"type": "result"}) == []


# ---------------------------------------------------------------------------
# read_agent_response() — real coroutine, stubbed stdout stream
# ---------------------------------------------------------------------------

class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)


def _jsonl(*events):
    return [(json.dumps(e) + "\n").encode() for e in events]


def test_read_agent_response_logs_permission_denials(monkeypatch, tmp_workspace, caplog):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    denial = {
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "tool_input": {"command": "curl -s https://example.com"},
    }
    agent_server.agent_processes["test-agent"] = _FakeProc(_jsonl(
        {"type": "result", "permission_denials": [denial], "usage": {}},
    ))

    with caplog.at_level(logging.WARNING, logger="agent-server"):
        text, metadata = asyncio.run(
            agent_server.read_agent_response("test-agent", "0")
        )

    assert metadata["permission_denials"] == [denial]
    assert any(
        "permission denied" in r.message and "Bash" in r.message and "curl" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_read_agent_response_no_denials_logs_nothing_extra(monkeypatch, tmp_workspace, caplog):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    agent_server.agent_processes["test-agent"] = _FakeProc(_jsonl(
        {"type": "result", "usage": {}},
    ))

    with caplog.at_level(logging.WARNING, logger="agent-server"):
        text, metadata = asyncio.run(
            agent_server.read_agent_response("test-agent", "0")
        )

    assert metadata["permission_denials"] == []
    assert not any("permission denied" in r.message for r in caplog.records)


def test_read_agent_response_logs_system_init_tool_list(monkeypatch, tmp_workspace, caplog):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")

    agent_server.agent_processes["test-agent"] = _FakeProc(_jsonl(
        {"type": "system", "subtype": "init", "tools": ["Read", "Bash"]},
        {"type": "result", "usage": {}},
    ))

    with caplog.at_level(logging.INFO, logger="agent-server"):
        asyncio.run(agent_server.read_agent_response("test-agent", "0"))

    assert any(
        "session tools" in r.message and "Read" in r.message and "Bash" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# start_agent_subprocess() — per-agent env
# ---------------------------------------------------------------------------

def _write_agent(tmp_workspace, name, extra_config=None):
    agent_dir = tmp_workspace / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "SYSTEM_PROMPT.md").write_text("You are a test agent.")

    agents_json_path = tmp_workspace / "config" / "agents.json"
    cfg = json.loads(agents_json_path.read_text())
    entry = {"system_prompt": f"agents/{name}/SYSTEM_PROMPT.md"}
    if extra_config:
        entry.update(extra_config)
    cfg["agents"][name] = entry
    agents_json_path.write_text(json.dumps(cfg))


def _spawn_and_capture(monkeypatch, tmp_workspace, agent_name):
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
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(
        agent_server.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    async def run():
        await agent_server.init_db()
        await agent_server.load_config()
        await agent_server.start_agent_subprocess(agent_name)
        await asyncio.sleep(0)

    asyncio.run(run())
    return captured


def test_start_agent_subprocess_applies_per_agent_env(monkeypatch, tmp_workspace):
    monkeypatch.setenv("KARAKOS_TEST_AMBIENT", "ambient-value")
    _write_agent(tmp_workspace, "env-agent",
                 {"env": {"KARAKOS_TEST_OVERRIDE": "override-value"}})

    captured = _spawn_and_capture(monkeypatch, tmp_workspace, "env-agent")

    env = captured["kwargs"].get("env")
    assert env is not None
    assert env["KARAKOS_TEST_OVERRIDE"] == "override-value"
    # Overrides layer onto the inherited environment rather than replacing it.
    assert env["KARAKOS_TEST_AMBIENT"] == "ambient-value"


def test_start_agent_subprocess_without_env_key_still_inherits_everything(
        monkeypatch, tmp_workspace):
    """No `env` key in the agent's config must still mean plain inherit.

    This used to assert `env is None`, which was the same guarantee spelled
    differently. Since #101 the spawn environment is always an explicit dict
    — the agent's own name has to reach the MCP tool server somehow, and the
    environment is the only channel a subprocess-of-a-subprocess has — so the
    property under test is that the dict is os.environ plus that one key, and
    never a replacement for it.
    """
    monkeypatch.setenv("KARAKOS_TEST_AMBIENT", "ambient-value")
    _write_agent(tmp_workspace, "plain-agent")

    captured = _spawn_and_capture(monkeypatch, tmp_workspace, "plain-agent")

    env = captured["kwargs"].get("env")
    assert env is not None
    assert env["KARAKOS_TEST_AMBIENT"] == "ambient-value", \
        "the spawn environment replaced the inherited one instead of layering onto it"
    assert env["KARAKOS_AGENT"] == "plain-agent"


# ---------------------------------------------------------------------------
# Real CLI — the issue's literal acceptance test
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skipif(CLAUDE_BIN is None, reason="claude CLI not installed on this box")
def test_real_claude_dispatch_denies_webfetch_when_settings_deny_it(tmp_path):
    """'Add a deny rule for WebFetch to settings. Ask the agent to fetch a
    URL. Pass = it declines.' Spawns the real CLI with a settings file
    whose only content is the deny rule, and inspects the live
    stream-json 'system'/'init' event — the CLI's own declaration of what
    tools this session actually has — for WebFetch's absence."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"deny": ["WebFetch"]}}))

    result = subprocess.run(
        [CLAUDE_BIN, "-p", "hi",
         "--settings", str(settings_path),
         "--dangerously-skip-permissions",
         "--output-format", "stream-json", "--verbose"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    parsed = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    init_events = [
        e for e in parsed if e.get("type") == "system" and e.get("subtype") == "init"
    ]
    assert init_events, f"no system/init event in output: {result.stdout[:2000]}"
    tools = init_events[0]["tools"]
    assert "WebFetch" not in tools, f"WebFetch still granted despite deny rule: {tools}"
