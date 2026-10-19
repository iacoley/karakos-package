"""bin/self-test.py: diagnosis plus one read-only tool call per MCP server."""
import json

import pytest

from conftest import import_script
from lib.graph.store import open_graph

st = import_script("self-test")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_URL", "http://127.0.0.1:9")  # nothing listens
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


def by_step(results):
    return {r["step"]: r for r in results}


def test_empty_workspace_fails_where_expected(ws):
    r = by_step(st.run(ws))
    assert r["self-diagnose"]["ok"] is False
    assert r["tools-server: initialize"]["ok"] and r["tools-server: tools/list"]["ok"]
    assert r["tools-server: taskboard"]["ok"] is True
    assert r["tools-server: memory"]["ok"] is False
    assert "karakos migrate" in r["tools-server: memory"]["detail"]
    assert r["admin-server: tools/list"]["ok"] is True
    assert r["admin-server: list_agents"]["ok"] is False
    assert "cannot reach" in r["admin-server: list_agents"]["detail"]


def test_memory_step_passes_once_the_graph_exists(ws):
    open_graph(ws / "data", create=True)
    assert by_step(st.run(ws))["tools-server: memory"]["ok"] is True


def test_exit_code_and_json_output(ws, capsys):
    assert st.main(["--workspace", str(ws), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and len(out["results"]) == 8


def test_text_output_marks_pass_and_fail(ws, capsys):
    st.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    assert "PASS tools-server: taskboard" in out and "FAIL admin-server: list_agents" in out


def test_server_that_cannot_start_is_a_failure(ws, monkeypatch):
    def boom(*a, **k):
        raise OSError("no python")
    monkeypatch.setattr(st.subprocess, "Popen", boom)
    r = [x for x in st.run(ws) if x["step"].endswith(": start")]
    assert len(r) == 2 and not any(x["ok"] for x in r)
