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
    assert tool_names == {"list_agents", "get_health", "reload_agent", "reset_agent", "create_agent"}


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
