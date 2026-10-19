"""Memory hygiene (#238 build A): forget, supersede, delete, merge, taskboard delete."""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from lib.graph import tools as gt
from lib.graph.store import open_graph
from tests.graph.helpers import _fresh_embedder  # noqa: F401  (autouse fixture)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    return open_graph(tmp_path, create=True)


def mem(store, **kw):
    return gt.memory_tool(kw, store, "tester")


def gr(store, **kw):
    return gt.graph_tool(kw, store, "tester")


def audit(store):
    with store.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM memory_audit ORDER BY id")]


def texts(store, q):
    return [r["text"] for r in mem(store, action="recall", query=q)["results"]]


def test_forget_hides_from_recall_and_audits(store):
    oid = mem(store, action="write", content="Mittens likes tuna")["id"]
    assert mem(store, action="forget", id=oid, reason="wrong cat")["forgotten"] is True
    assert texts(store, "tuna") == []
    a = audit(store)
    assert a[0]["action"] == "forget" and a[0]["reason"] == "wrong cat" and a[0]["agent"] == "tester"
    with store.read() as c:  # soft: the row is still there
        assert c.execute("SELECT archived_at FROM observations WHERE id=?", (oid,)).fetchone()[0]


def test_forget_errors(store):
    assert "no observation" in mem(store, action="forget", id=999)["error"]
    assert "'id'" in mem(store, action="forget")["error"]
    oid = mem(store, action="write", content="x fact")["id"]
    mem(store, action="forget", id=oid)
    assert "already forgotten" in mem(store, action="forget", id=oid)["error"]


def test_supersede_replaces_and_keeps_metadata(store):
    old = mem(store, action="write", content="Server runs on port 80", subject="Server",
              importance=9, domain="ops")["id"]
    out = mem(store, action="supersede", id=old, text="Server runs on port 8080")
    assert out["superseded"] == old and out["id"] != old
    assert texts(store, "port") == ["Server runs on port 8080"]
    with store.read() as c:
        new = c.execute("SELECT * FROM observations WHERE id=?", (out["id"],)).fetchone()
        assert new["importance"] == 9 and new["domain"] == "ops" and new["entity_id"]
        assert c.execute("SELECT superseded_by FROM observations WHERE id=?",
                         (old,)).fetchone()[0] == out["id"]
    assert audit(store)[0]["action"] == "supersede"
    assert "already" in mem(store, action="supersede", id=old, text="again")["error"]
    assert "'text'" in mem(store, action="supersede", id=out["id"], text=" ")["error"]


def test_delete_entity_is_soft_and_drops_edges(store):
    gr(store, action="add_edge", src="Alice", dst="Bob", relation="knows")
    out = gr(store, action="delete_entity", name="Bob", reason="dup")
    assert out["deleted"] is True and out["edges_removed"] == 1
    assert "no entity" in gr(store, action="delete_entity", name="Bob")["error"]
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
        assert c.execute("SELECT archived_at FROM entities WHERE name='Bob'").fetchone()[0]
    d = audit(store)[0]
    assert d["action"] == "delete_entity" and "knows" in d["detail"]
    # adding the name again brings it back rather than hitting the unique key
    again = gr(store, action="add_entity", name="Bob")
    assert again["created"] is False and again["id"] == out["id"]
    with store.read() as c:
        assert c.execute("SELECT archived_at FROM entities WHERE id=?",
                         (out["id"],)).fetchone()[0] is None


def test_edge_to_deleted_entity_id_is_refused(store):
    gr(store, action="add_entity", name="Gone")
    eid = gr(store, action="add_entity", name="Gone")["id"]
    gr(store, action="delete_entity", name="Gone")
    r = gr(store, action="add_edge", src=eid, dst="Other", relation="knows")
    assert "error" in r


def test_delete_edge(store):
    gr(store, action="add_edge", src="A", dst="B", relation="works with")
    assert gr(store, action="delete_edge", src="A", dst="B", relation="works with")["deleted"]
    assert "no such edge" in gr(store, action="delete_edge", src="A", dst="B",
                                relation="works_with")["error"]
    assert audit(store)[0]["action"] == "delete_edge"
    assert "'relation'" in gr(store, action="delete_edge", src="A", dst="B")["error"]


def test_merge_moves_everything_and_aliases_the_old_name(store):
    gr(store, action="add_entity", name="Bobby", aliases=["Rob"])
    gr(store, action="add_entity", name="Robert")
    mem(store, action="write", content="Bobby plays chess", subject="Bobby")
    gr(store, action="add_edge", src="Bobby", dst="Chess", relation="plays")
    gr(store, action="add_edge", src="Robert", dst="Chess", relation="plays")  # duplicate
    gr(store, action="add_edge", src="Bobby", dst="Robert", relation="same_as")  # self-edge
    out = gr(store, action="merge_entities", **{"from": "Bobby", "into": "Robert"})
    assert out["observations"] == 1 and out["edges_dropped"] == 2 and out["edges"] == 0
    assert texts(store, "chess") == ["Bobby plays chess"]
    assert mem(store, action="recall", query="chess", entity="Robert")["results"]
    for name in ("Bobby", "Rob"):
        assert store.get_entity(name)["name"] == "Robert"
    with store.read() as c:
        assert c.execute("SELECT archived_at FROM entities WHERE name='Bobby'").fetchone()[0]
    assert audit(store)[-1]["action"] == "merge_entities"


def test_merge_errors(store):
    gr(store, action="add_entity", name="Solo")
    assert "itself" in gr(store, action="merge_entities", **{"from": "Solo", "into": "solo"})["error"]
    assert "no entity" in gr(store, action="merge_entities", **{"from": "Nope", "into": "Solo"})["error"]
    assert "'into'" in gr(store, action="merge_entities", **{"from": "Solo"})["error"]


def test_audit_table_is_created_lazily_on_an_older_graph(store):
    with store.write() as c:
        c.execute("DROP TABLE memory_audit")
    oid = mem(store, action="write", content="old graph fact")["id"]
    assert mem(store, action="forget", id=oid)["forgotten"] is True
    assert len(audit(store)) == 1


# -- taskboard -------------------------------------------------------------

@pytest.fixture
def tools(tmp_path):
    (tmp_path / "data").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "tools_hygiene_under_test",
            Path(__file__).parent.parent / "mcp" / "tools-server.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["tools_hygiene_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return m


def test_taskboard_delete_tombstones(tools, tmp_path):
    tb = lambda **a: tools.handle_core_tool("taskboard", a)  # noqa: E731
    t = tb(action="add", title="junk")["task"]
    assert tb(action="delete", id=t["id"])["deleted"] is True
    assert tb(action="list")["tasks"] == []
    assert "not found" in tb(action="delete", id=t["id"])["error"]
    assert "not found" in tb(action="complete", id=t["id"])["error"]
    assert "not found" in tb(action="update", id=t["id"], status="x")["error"]
    raw = (tmp_path / "data" / "taskboard.json").read_text()
    assert "deleted_at" in raw  # tombstone kept in the file


def test_tool_schemas_advertise_the_new_actions(tools):
    by = {t["name"]: t for t in tools.CORE_TOOLS}
    assert {"forget", "supersede"} <= set(by["memory"]["inputSchema"]["properties"]["action"]["enum"])
    assert {"delete_entity", "delete_edge", "merge_entities"} <= set(
        by["graph"]["inputSchema"]["properties"]["action"]["enum"])
    assert "delete" in by["taskboard"]["inputSchema"]["properties"]["action"]["enum"]
