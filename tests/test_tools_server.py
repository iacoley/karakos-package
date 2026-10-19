"""
Tests for mcp/tools-server.py skill discovery (issues #83, #84).

#83: discover_skills() must actually find the shipped example skill, and
     the server that hosts it must be registered where Claude Code reads
     config from (covered by test_admin_mcp.py::test_mcp_config_at_repo_root).
#84: a skill directory with a SKILL.md but no tools.json (the frontmatter
     Agent Skills convention) must produce a startup diagnostic naming the
     file, instead of being silently invisible.
"""

import json

import pytest

from conftest import import_script, PACKAGE_ROOT


@pytest.fixture
def tools_server(monkeypatch, tmp_path):
    """Import tools-server.py with WORKSPACE_ROOT pointed at a scratch dir."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def test_shipped_hello_world_skill_is_discoverable():
    """The package's only shipped skill must live where discover_skills()
    actually looks — one level under skills/ (skills/<name>/tools.json).

    Regression for #83: it used to ship at skills/examples/hello-world/,
    two levels deep, invisible to the one-level scan.
    """
    tools_json = PACKAGE_ROOT / "skills" / "hello-world" / "tools.json"
    assert tools_json.exists(), (
        "expected skills/hello-world/tools.json — the example skill must "
        "sit one level under skills/ to match discover_skills()'s scan depth"
    )
    assert not (PACKAGE_ROOT / "skills" / "examples").exists(), (
        "skills/examples/ should be gone now that hello-world moved up a level"
    )


def test_discover_skills_finds_real_hello_world(tools_server, monkeypatch):
    """discover_skills() against the real repo layout must surface hello_world."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(PACKAGE_ROOT))
    mod = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    tools = mod.discover_skills()
    names = [t["name"] for t in tools]
    assert "hello_world" in names


def test_discover_skills_loads_tools_json_skill(tools_server, tmp_path):
    """A normal skills/<name>/tools.json skill is discovered and dispatchable."""
    skill_dir = tmp_path / "skills" / "my-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "tools.json").write_text(json.dumps({
        "skill_name": "my-skill",
        "version": "1.0.0",
        "description": "test skill",
        "tools": [{
            "name": "my_tool",
            "description": "does a thing",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }],
    }))

    tools = tools_server.discover_skills()
    assert [t["name"] for t in tools] == ["my_tool"]
    assert tools[0]["_skill_dir"] == str(skill_dir)


def test_frontmatter_only_skill_is_skipped_with_diagnostic(tools_server, tmp_path, capsys):
    """Issue #84: a SKILL.md with no tools.json must not vanish silently —
    stderr must name the specific file and say why it was skipped."""
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: foo\ndescription: a frontmatter-style skill\n---\n\nBody text.\n"
    )

    tools = tools_server.discover_skills()

    assert tools == []
    err = capsys.readouterr().err
    assert str(skill_md) in err
    assert "tools.json" in err


def test_skill_dir_with_neither_file_is_silently_ignored(tools_server, tmp_path, capsys):
    """A directory under skills/ with no tools.json and no SKILL.md (e.g. an
    organizational folder like the old skills/examples/) isn't a skill at
    all, so it must not be flagged as one."""
    (tmp_path / "skills" / "not-a-skill").mkdir(parents=True)

    tools = tools_server.discover_skills()

    assert tools == []
    assert capsys.readouterr().err == ""


# --- memory / graph tool definitions (ANDURIL 4.2) ---------------------------

def _core(tools_server, name):
    return next(t for t in tools_server.CORE_TOOLS if t["name"] == name)


def test_core_tools_list_memory_and_graph_with_new_enums(tools_server):
    mem = _core(tools_server, "memory")["inputSchema"]["properties"]["action"]["enum"]
    assert mem == ["write", "recall", "status", "forget", "supersede", "remember", "facts",
                   "recent"]
    assert _core(tools_server, "graph")["inputSchema"]["properties"]["action"]["enum"] == \
        ["add_entity", "add_edge", "delete_entity", "delete_edge", "merge_entities"]


def test_validate_args_rejects_unknown_action_and_non_string_content(tools_server):
    schema = _core(tools_server, "memory")["inputSchema"]
    assert "action" in tools_server.validate_args({"action": "bogus"}, schema)
    assert "content" in tools_server.validate_args({"action": "write", "content": 5}, schema)
    gschema = _core(tools_server, "graph")["inputSchema"]
    assert tools_server.validate_args({"action": "bogus"}, gschema)


def test_health_loop_exists_and_write_health_writes_timestamp(tools_server, monkeypatch, tmp_path):
    """_health_loop is the background refresher; it is not started here (infinite loop).
    One write_health() call must produce the health file the monitor reads."""
    import json as _json

    assert callable(tools_server._health_loop)
    health = tmp_path / "data" / "health" / "mcp-tools.json"
    monkeypatch.setattr(tools_server, "HEALTH_FILE", health)
    tools_server.write_health()
    assert "timestamp" in _json.loads(health.read_text())


def test_workspace_status_reports_installed_version_from_version_file(tools_server, tmp_path):
    """`workspace status` reads the release from the VERSION file at the workspace
    root, not from .karakos/config.json (which setup never rewrites on upgrade)."""
    (tmp_path / "VERSION").write_text("2.0.2\n")
    (tmp_path / ".karakos").mkdir()
    (tmp_path / ".karakos" / "config.json").write_text(json.dumps({"version": "1.0.0"}))

    result = tools_server.handle_core_tool("workspace", {"action": "status"})

    assert result["installed_version"] == "2.0.2"
    assert result["version"] == result["installed_version"]
    assert result["schema_version"] is None


def test_workspace_status_reads_schema_version_stamp(tools_server, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".schema-version").write_text(json.dumps({"schema": 5}))

    result = tools_server.handle_core_tool("workspace", {"action": "status"})

    assert result["schema_version"] == 5
