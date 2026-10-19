"""
Tests for mcp/admin-server.py — karakos-admin MCP.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT

ADMIN_SERVER = PACKAGE_ROOT / "mcp" / "admin-server.py"


@pytest.fixture
def admin():
    return import_script("admin-server", file_path=ADMIN_SERVER)


def _rpc(lines):
    """Send JSON lines to admin-server, return parsed responses."""
    proc = subprocess.run(
        [sys.executable, str(ADMIN_SERVER)],
        input="\n".join(json.dumps(l) for l in lines),
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "AGENT_SERVER_TOKEN": ""},
    )
    return [json.loads(line) for line in proc.stdout.strip().split("\n") if line]


def test_admin_server_exists():
    assert ADMIN_SERVER.exists()
    assert os.access(ADMIN_SERVER, os.X_OK)


def test_initialize():
    [resp] = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    ])
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == "karakos-admin"
    assert "tools" in resp["result"]["capabilities"]


def test_initialized_notification_no_response():
    """notifications/initialized must not produce a response (it's a notification)."""
    responses = _rpc([
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    # Only the tools/list response, not the notification
    assert len(responses) == 1
    assert responses[0]["id"] == 2


def test_tools_list():
    [resp] = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ])
    tool_names = {t["name"] for t in resp["result"]["tools"]}
    assert tool_names == {
        "list_agents", "get_health", "reload_agent", "reset_agent", "create_agent",
        "get_usage", "get_cost", "tail_log", "set_effort", "get_queue",
        "pause_agent", "resume_agent", "get_findings",
    }


def test_unknown_method():
    [resp] = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "bogus/method"},
    ])
    assert resp["error"]["code"] == -32601


def test_unknown_tool():
    [resp] = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
    ])
    assert resp["error"]["code"] == -32601


def test_invalid_agent_name(admin):
    """Name validation must reject names that don't match the lowercase regex."""
    with pytest.raises(ValueError):
        admin.tool_reload_agent({"name": "Bad-Name"})
    with pytest.raises(ValueError):
        admin.tool_reset_agent({"name": "with spaces"})
    with pytest.raises(ValueError):
        admin.tool_create_agent({"name": "TOO_UPPER"})


def test_missing_token_propagates(admin, monkeypatch):
    monkeypatch.setattr(admin, "TOKEN", "")
    with pytest.raises(RuntimeError, match="AGENT_SERVER_TOKEN"):
        admin.tool_list_agents({})


def test_parse_error():
    """Malformed JSON should produce a parse error response."""
    proc = subprocess.run(
        [sys.executable, str(ADMIN_SERVER)],
        input="not json\n",
        capture_output=True, text=True, timeout=5,
    )
    resp = json.loads(proc.stdout.strip())
    assert resp["error"]["code"] == -32700


def test_mcp_config_at_repo_root():
    """Spec requires .mcp.json at repo root so spawned claude subprocesses pick it up.

    system-tools must live here too (issue #83) — Claude Code only reads
    .mcp.json from the project root, never mcp/.mcp.json, so a second config
    file anywhere else is invisible to the agent.
    """
    cfg = PACKAGE_ROOT / ".mcp.json"
    assert cfg.exists(), ".mcp.json must exist at repo root"
    data = json.loads(cfg.read_text())
    assert "karakos-admin" in data["mcpServers"]
    assert "system-tools" in data["mcpServers"]
    assert not (PACKAGE_ROOT / "mcp" / ".mcp.json").exists(), (
        "mcp/.mcp.json is dead config Claude Code never reads; keeping it "
        "invites re-registering a server there instead of at the root"
    )


# ---------------------------------------------------------------------------
# Managing yourself (MÍRDAIN G1)
# ---------------------------------------------------------------------------

@pytest.fixture
def no_caller(monkeypatch):
    monkeypatch.delenv("KARAKOS_AGENT", raising=False)


@pytest.fixture
def http_calls(admin, monkeypatch):
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(admin, "_http", fake)
    return calls


def _write_agents(root: Path, roles: dict) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    body = "version: 2\nagents:\n" + "".join(
        f"  {name}:\n    role: {role}\n" for name, role in roles.items())
    (root / "config" / "agents.yaml").write_text(body)


def test_tail_log_clamps_to_200(admin, monkeypatch, tmp_path, no_caller):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "relay.log").write_text(
        "".join(f"line {i}\n" for i in range(300)))
    out = admin.tool_tail_log({"service": "relay", "lines": 500})
    assert out["service"] == "relay"
    assert out["lines"] == 200
    assert len(out["text"].splitlines()) == 200
    assert out["text"].splitlines()[-1] == "line 299"


def test_tail_log_default_is_40(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "relay.log").write_text("".join(f"l{i}\n" for i in range(300)))
    assert admin.tool_tail_log({"service": "relay"})["lines"] == 40


def test_tail_log_rejects_traversal(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    with pytest.raises(ValueError):
        admin.tool_tail_log({"service": "../x"})
    with pytest.raises(ValueError):
        admin.tool_tail_log({"service": "a..b"})


def test_tail_log_rejects_traversal_over_rpc(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    [resp] = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "tail_log", "arguments": {"service": "../x"}}},
    ])
    assert resp["error"]["code"] == -32603


def test_tail_log_missing_lists_available(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "relay.log").write_text("x\n")
    (tmp_path / "logs" / "agent-server.log").write_text("x\n")
    out = admin.tool_tail_log({"service": "nope"})
    assert out["error"] == "no log named nope"
    assert out["available"] == ["agent-server", "relay"]


def test_redact_masks_secret(admin, monkeypatch):
    """tail_log output goes through lib/redact.py; the real package lib is used here."""
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", PACKAGE_ROOT)
    secret = "sk-ant-" + "A" * 30
    out = admin._redact(f"auth {secret}\n")
    assert secret not in out
    assert "[redacted]" in out


def test_check_scope_unset_is_allowed(admin, monkeypatch, tmp_path, no_caller):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    admin._check_scope("marvin")  # operator/CLI: no raise


def test_check_scope_self_is_allowed(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("KARAKOS_AGENT", "worker")
    admin._check_scope("worker")


def test_check_scope_builder_cannot_touch_other(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    _write_agents(tmp_path, {"worker": "builder", "marvin": "monitor"})
    monkeypatch.setenv("KARAKOS_AGENT", "worker")
    with pytest.raises(PermissionError, match="may only manage itself"):
        admin._check_scope("marvin")


def test_check_scope_primary_can_touch_other(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    _write_agents(tmp_path, {"amos": "primary", "marvin": "monitor"})
    monkeypatch.setenv("KARAKOS_AGENT", "amos")
    admin._check_scope("marvin")


def test_check_scope_unreadable_registry_denies(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)  # no config/agents.yaml
    monkeypatch.setenv("KARAKOS_AGENT", "worker")
    with pytest.raises(PermissionError):
        admin._check_scope("marvin")


def test_scope_denial_over_rpc(tmp_path, monkeypatch):
    _write_agents(tmp_path, {"worker": "builder"})
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("KARAKOS_AGENT", "worker")
    [resp] = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "pause_agent", "arguments": {"name": "marvin"}}},
    ])
    assert resp["error"]["code"] == -32603
    assert "may only manage itself" in resp["error"]["message"]


def test_reload_and_reset_scoped_to_self(admin, http_calls, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    _write_agents(tmp_path, {"worker": "builder"})
    monkeypatch.setenv("KARAKOS_AGENT", "worker")
    for tool in (admin.tool_reload_agent, admin.tool_reset_agent):
        with pytest.raises(PermissionError):
            tool({"name": "marvin"})
    assert http_calls == []
    admin.tool_reload_agent({"name": "worker"})
    admin.tool_reset_agent({"name": "worker"})
    assert http_calls == [
        ("POST", "/agents/worker/reload", None),
        ("POST", "/agents/worker/reset", None),
    ]


def test_reload_and_reset_unscoped_for_operator(admin, http_calls, no_caller):
    admin.tool_reload_agent({"name": "marvin"})
    admin.tool_reset_agent({"name": "marvin"})
    assert http_calls == [
        ("POST", "/agents/marvin/reload", None),
        ("POST", "/agents/marvin/reset", None),
    ]


def test_get_cost_all_and_one(admin, http_calls, no_caller):
    admin.tool_get_cost({})
    admin.tool_get_cost({"agent": "marvin"})
    assert http_calls == [("GET", "/cost", None), ("GET", "/cost/marvin", None)]


def test_get_cost_rejects_bad_name(admin, http_calls):
    with pytest.raises(ValueError):
        admin.tool_get_cost({"agent": "Bad Name"})
    assert http_calls == []


def test_set_effort_posts_level(admin, http_calls, no_caller):
    admin.tool_set_effort({"name": "marvin", "level": "high"})
    assert http_calls == [("POST", "/agents/marvin/effort", {"level": "high"})]


def test_set_effort_rejects_unknown_level(admin, http_calls, no_caller):
    with pytest.raises(ValueError):
        admin.tool_set_effort({"name": "marvin", "level": "ultra"})
    assert http_calls == []


def test_set_effort_scoped_to_self(admin, http_calls, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    _write_agents(tmp_path, {"worker": "builder"})
    monkeypatch.setenv("KARAKOS_AGENT", "worker")
    with pytest.raises(PermissionError):
        admin.tool_set_effort({"name": "marvin", "level": "low"})
    admin.tool_set_effort({"name": "worker", "level": "low"})
    assert http_calls == [("POST", "/agents/worker/effort", {"level": "low"})]


def test_pause_agent_body(admin, http_calls, monkeypatch, no_caller):
    admin.tool_pause_agent({"name": "marvin", "minutes": 30})
    admin.tool_pause_agent({"name": "marvin"})
    monkeypatch.setenv("KARAKOS_AGENT", "marvin")
    admin.tool_pause_agent({"name": "marvin", "minutes": 5})
    assert http_calls == [
        ("POST", "/agents/marvin/pause", {"minutes": 30, "by": "agent"}),
        ("POST", "/agents/marvin/pause", {"minutes": None, "by": "agent"}),
        ("POST", "/agents/marvin/pause", {"minutes": 5, "by": "marvin"}),
    ]


def test_pause_agent_rejects_out_of_range(admin, http_calls, no_caller):
    for bad in (0, 1441, True, "10"):
        with pytest.raises(ValueError):
            admin.tool_pause_agent({"name": "marvin", "minutes": bad})
    assert http_calls == []


def test_resume_agent_posts_empty_body(admin, http_calls, no_caller):
    admin.tool_resume_agent({"name": "marvin"})
    assert http_calls == [("POST", "/agents/marvin/resume", {})]


def test_get_queue_and_usage_paths(admin, http_calls, no_caller):
    admin.tool_get_queue({"name": "marvin"})
    admin.tool_get_usage({})
    assert http_calls == [("GET", "/agents/marvin/queue", None), ("GET", "/usage", None)]


def test_get_findings_missing_and_present(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "WORKSPACE_ROOT", tmp_path)
    assert admin.tool_get_findings({}) == {"summary": None, "note": "no findings file yet"}
    (tmp_path / "data" / "health").mkdir(parents=True)
    (tmp_path / "data" / "health" / "summary.md").write_text("all green\n")
    assert admin.tool_get_findings({}) == {"summary": "all green\n"}
