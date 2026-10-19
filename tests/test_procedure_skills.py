"""Procedure skills (#235 build A): scan, read, validate."""
import json
import sys

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT))
from lib import procedure_skills as ps  # noqa: E402

SERVER = PACKAGE_ROOT / "mcp" / "tools-server.py"


def proc(root, name, fm=None, body="Steps.\n"):
    d = root / "skills" / name
    d.mkdir(parents=True)
    fm = fm if fm is not None else f"---\nname: {name}\ndescription: does {name}\n---\n"
    (d / "SKILL.md").write_text(fm + body)
    return d


def tool(root, name, tool_name="echo", script=True, body='import json;print(json.dumps({"ok": 1}))'):
    d = root / "skills" / name
    (d / "scripts").mkdir(parents=True)
    (d / "tools.json").write_text(json.dumps({
        "skill_name": name, "version": "1.0.0", "description": "t",
        "tools": [{"name": tool_name, "description": "d",
                   "inputSchema": {"type": "object", "properties": {}}}]}))
    if script:
        (d / "scripts" / f"{tool_name}.py").write_text(body + "\n")
    return d


def test_frontmatter_parse():
    assert ps.parse_frontmatter("---\nname: a\ndescription: 'b c'\n---\nx") == {"name": "a", "description": "b c"}
    assert ps.parse_frontmatter("no block") is None


def test_scan_lists_both_kinds(tmp_path):
    proc(tmp_path, "alpha")
    tool(tmp_path, "beta")
    (tmp_path / "skills" / "empty").mkdir()
    got = {s["name"]: s for s in ps.scan(tmp_path / "skills")}
    assert set(got) == {"alpha", "beta"}
    assert got["alpha"]["kind"] == "procedure" and got["alpha"]["description"] == "does alpha"
    assert got["beta"]["kind"] == "tool" and got["beta"]["description"] == "t"


def test_read_returns_text_and_rejects_bad_names(tmp_path):
    proc(tmp_path, "alpha")
    assert "Steps." in ps.read(tmp_path / "skills", "alpha")["content"]
    for bad in ("../x", "A b", "", None):
        with pytest.raises(ValueError):
            ps.read(tmp_path / "skills", bad)
    with pytest.raises(LookupError):
        ps.read(tmp_path / "skills", "missing")


def test_validate_procedure_skill(tmp_path):
    proc(tmp_path, "good")
    assert ps.validate(tmp_path / "skills", "good")["ok"] is True
    proc(tmp_path, "wrongname", fm="---\nname: other\ndescription: d\n---\n")
    assert any("!= directory" in p for p in ps.validate(tmp_path / "skills", "wrongname")["problems"])
    proc(tmp_path, "nofm", fm="")
    assert any("no frontmatter" in p for p in ps.validate(tmp_path / "skills", "nofm")["problems"])
    proc(tmp_path, "nodesc", fm="---\nname: nodesc\n---\n")
    assert any("description" in p for p in ps.validate(tmp_path / "skills", "nodesc")["problems"])


def test_validate_tool_skill_runs_test_tool(tmp_path):
    tool(tmp_path, "ok-skill")
    r = ps.validate(tmp_path / "skills", "ok-skill", server=SERVER, workspace=tmp_path)
    assert r["ok"], r
    assert r["tools"] == ["echo"]


def test_validate_flags_missing_script_and_schema(tmp_path):
    tool(tmp_path, "noscript", script=False)
    r = ps.validate(tmp_path / "skills", "noscript", server=SERVER, workspace=tmp_path)
    assert not r["ok"] and any("no scripts/echo" in p for p in r["problems"])
    d = tool(tmp_path, "badjson")
    (d / "tools.json").write_text(json.dumps({"skill_name": "other", "tools": []}))
    r = ps.validate(tmp_path / "skills", "badjson")
    assert any("missing 'version'" in p for p in r["problems"])
    assert any("skill_name" in p for p in r["problems"])


def test_validate_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "TEST_TIMEOUT_S", 1)
    tool(tmp_path, "slow", body="import time;time.sleep(30)")
    r = ps.validate(tmp_path / "skills", "slow", server=SERVER, workspace=tmp_path)
    assert any("timed out" in p for p in r["problems"])


def test_skill_tool_is_registered_with_actions():
    src = (PACKAGE_ROOT / "mcp" / "tools-server.py").read_text()
    assert '"name": "skill"' in src and '"list", "read", "validate"' in src
