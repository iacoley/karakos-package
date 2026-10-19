"""memory and graph MCP tools over lib/graph (ANDURIL 4.2).

Replaces the remember/facts/recent/connection-closing cases of the 1.x
test_semantic_recall.py. No model is loaded: fastembed is faked or removed.
"""
import sqlite3
import sys

import pytest

from conftest import PACKAGE_ROOT, import_script
from lib.graph import embed
from lib.graph import tools as gt
from lib.graph.store import open_graph
from tests.graph.helpers import FakeTextEmbedding, install_fastembed
from tests.graph.helpers import _fresh_embedder  # noqa: F401  (autouse fixture)


@pytest.fixture
def store(tmp_path):
    return open_graph(tmp_path, create=True)


def no_model(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)


def mem(store, **kw):
    return gt.memory_tool(kw, store, "tester")


def gr(store, **kw):
    return gt.graph_tool(kw, store, "tester")


# --- write / recall --------------------------------------------------------

def test_write_fact_then_recall_returns_it(store, monkeypatch):
    no_model(monkeypatch)
    w = mem(store, action="write", content="The cat likes tuna", subject="Mittens")
    assert w["status"] == "ok" and w["created"] is True and w["entity_id"]
    r = mem(store, action="recall", query="tuna")
    assert r["results"][0]["text"] == "The cat likes tuna"
    assert r["results"][0]["entity"] == "Mittens"
    assert r["results"][0]["type"] == "observation"
    ent = {e["name"]: e for e in r["entities"]}
    assert "Mittens" in ent and ent["Mittens"]["edges"] == []
    with store.read() as c:
        row = c.execute("SELECT * FROM observations WHERE id=?", (w["id"],)).fetchone()
        ek = c.execute("SELECT kind FROM entities WHERE id=?", (w["entity_id"],)).fetchone()
    assert row["source"] == "write" and row["agent"] == "tester"
    assert row["importance"] == 8.0 and ek["kind"] == "topic"


def test_default_importance_by_kind(store, monkeypatch):
    no_model(monkeypatch)
    ids = {k: mem(store, action="write", content=f"c {k}", kind=k, subkind="s")["id"]
           for k in ("fact", "episode", "pattern")}
    with store.read() as c:
        imp = {k: c.execute("SELECT importance FROM observations WHERE id=?", (i,))
               .fetchone()[0] for k, i in ids.items()}
    assert imp == {"fact": 8.0, "episode": 5.0, "pattern": 6.0}


def test_duplicate_write_is_idempotent(store, monkeypatch):
    no_model(monkeypatch)
    a = mem(store, action="write", content="Same thing", subject="X")
    b = mem(store, action="write", content="  same   THING ", subject="X")
    assert a["created"] is True and b["created"] is False and a["id"] == b["id"]


def test_write_validation(store):
    assert "error" in mem(store, action="write", content="   ")
    assert "error" in mem(store, action="write", content="x" * 4001)
    assert "error" in mem(store, action="write", content=5)
    assert "error" in mem(store, action="write", content="x", kind="bogus")
    assert "subkind" in mem(store, action="write", content="x", kind="pattern")["error"]


def test_confidence_clamped(store, monkeypatch):
    no_model(monkeypatch)
    i = mem(store, action="write", content="hi", confidence=7)["id"]
    j = mem(store, action="write", content="yo", confidence=-3)["id"]
    with store.read() as c:
        vals = [c.execute("SELECT confidence FROM observations WHERE id=?", (x,))
                .fetchone()[0] for x in (i, j)]
    assert vals == [1.0, 0.0]


def test_write_without_embedder_stores_null(store, monkeypatch):
    no_model(monkeypatch)
    w = mem(store, action="write", content="no model here")
    assert w["embedded"] is False
    with store.read() as c:
        assert c.execute("SELECT embedding FROM observations WHERE id=?",
                         (w["id"],)).fetchone()[0] is None


def test_write_with_embedder_stores_blob(store, monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding)
    w = mem(store, action="write", content="has model")
    assert w["embedded"] is True
    with store.read() as c:
        assert c.execute("SELECT embedding FROM observations WHERE id=?",
                         (w["id"],)).fetchone()[0]


def test_pattern_subkind_stored(store, monkeypatch):
    no_model(monkeypatch)
    w = mem(store, action="write", content="always do X", kind="pattern", subkind="Habit")
    with store.read() as c:
        assert c.execute("SELECT subkind FROM observations WHERE id=?",
                         (w["id"],)).fetchone()[0] == "habit"


def test_recall_modes(store, monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding)
    mem(store, action="write", content="alpha beta")
    assert mem(store, action="recall", query="alpha")["mode"] == "hybrid"
    embed._reset()
    no_model(monkeypatch)
    r = mem(store, action="recall", query="alpha")
    assert r["mode"] == "keyword" and r["reason"]
    r = mem(store, action="recall", mode="recent", query="ignored zzz")
    assert r["mode"] == "recent" and r["results"][0]["text"] == "alpha beta"
    assert "error" in mem(store, action="recall")
    assert "error" in mem(store, action="recall", query="a", mode="nope")
    assert "error" in mem(store, action="recall", query="a", kinds=["zzz"])


def test_recall_filters_limit_and_edges(store, monkeypatch):
    no_model(monkeypatch)
    mem(store, action="write", content="fact about Bob", subject="Bob", domain="d1")
    mem(store, action="write", content="episode about Bob", subject="Bob", kind="episode")
    gr(store, action="add_edge", src="Bob", dst="Acme", relation="works at")
    r = mem(store, action="recall", query="Bob", kinds=["fact"])
    assert [x["kind"] for x in r["results"]] == ["fact"]
    bob = next(e for e in r["entities"] if e["name"] == "Bob")
    assert bob["edges"] == [{"relation": "works_at", "to": "Acme", "weight": 1.0}]
    assert len(mem(store, action="recall", query="Bob", limit=1)["results"]) == 1
    r = mem(store, action="recall", query="Bob", entity="bob", domain="d1")
    assert len(r["results"]) == 1


def test_recall_touches_last_seen_and_survives_failure(store, monkeypatch):
    no_model(monkeypatch)
    mem(store, action="write", content="about Zed", subject="Zed")
    with store.write() as c:
        c.execute("UPDATE entities SET last_seen_at='2000-01-01T00:00:00Z'")
    mem(store, action="recall", query="Zed")
    with store.read() as c:
        assert c.execute("SELECT last_seen_at FROM entities").fetchone()[0] > "2001"
    monkeypatch.setattr(store, "write", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert "results" in mem(store, action="recall", query="Zed")


# --- status ---------------------------------------------------------------

def test_status_counts_and_never_loads_model(store, monkeypatch):
    mem(store, action="write", content="f1")
    mem(store, action="write", content="e1", kind="episode")
    gr(store, action="add_edge", src="a", dst="b", relation="r")
    for mod in [m for m in sys.modules if m.startswith("fastembed")]:
        monkeypatch.delitem(sys.modules, mod)

    monkeypatch.setattr(embed, "get_embedder", lambda: (_ for _ in ()).throw(
        AssertionError("model loaded")))
    s = mem(store, action="status")
    assert s["observations"] == {"fact": 1, "episode": 1, "pattern": 0, "archived": 0}
    assert s["entities"] >= 2 and s["edges"] == 1
    assert s["embedded"] + s["unembedded"] == 2
    assert s["embed_dim"] == embed.DIM and s["schema"] == 1
    assert s["db_bytes"] > 0
    for k in ("embed_model", "embedder_available", "last_consolidation", "migrated_from"):
        assert k in s


def test_status_does_not_import_fastembed(store, monkeypatch):
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    real = __import__

    def guarded(name, *a, **k):
        if name.split(".")[0] == "fastembed":
            raise AssertionError("fastembed imported")
        return real(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", guarded)
    assert "observations" in mem(store, action="status")


# --- deprecated aliases ---------------------------------------------------

def test_remember_alias_shape_and_errors(store, monkeypatch):
    no_model(monkeypatch)
    r = mem(store, action="remember", subject="Owner", content="Likes dark mode",
            domain="prefs", confidence=0.9)
    assert r == {"status": "ok", "id": r["id"], "subject": "Owner",
                 "content": "Likes dark mode", "confidence": 0.9, "domain": "prefs",
                 "deprecated": "use memory.write"}
    d = mem(store, action="remember", subject="Owner", content="x")
    assert d["confidence"] == 0.8 and d["domain"] == "general"
    assert mem(store, action="remember", subject="", content="x")["error"] == \
        "remember requires a non-empty 'subject'"
    assert mem(store, action="remember", subject="S", content="  ")["error"] == \
        "remember requires non-empty 'content'"
    assert mem(store, action="remember", content="x")["error"] == \
        "remember requires a non-empty 'subject'"


def test_facts_alias_shape(store, monkeypatch):
    no_model(monkeypatch)
    mem(store, action="remember", subject="Owner", content="Likes dark mode", domain="prefs")
    mem(store, action="write", content="dark episode", kind="episode")
    r = mem(store, action="facts", query="dark mode")
    assert set(r) == {"facts", "deprecated"} and r["deprecated"].startswith("use memory.")
    assert r["facts"] == [{"id": r["facts"][0]["id"], "subject": "Owner",
                           "content": "Likes dark mode", "confidence": 0.8,
                           "domain": "prefs"}]


def test_recent_alias_shape(store, monkeypatch):
    no_model(monkeypatch)
    mem(store, action="write", content="an episode", kind="episode")
    mem(store, action="write", content="a fact")
    r = mem(store, action="recent")
    assert set(r) == {"episodes", "deprecated"}
    assert [e["summary"] for e in r["episodes"]] == ["an episode"]
    assert set(r["episodes"][0]) == {"id", "summary", "importance", "created_at"}


def test_unknown_action_errors(store):
    assert "error" in mem(store, action="nonsense")
    assert "error" in gr(store, action="nonsense")


# --- graph ----------------------------------------------------------------

def test_add_entity_upsert_by_alias_and_summary(store):
    a = gr(store, action="add_entity", name="Robert", kind="Person", aliases=["Bob"],
           summary="short")
    assert a["created"] is True
    b = gr(store, action="add_entity", name="Bob", kind="person", aliases=["Bobby"],
           summary="a longer summary")
    assert b == {"id": a["id"], "created": False}
    gr(store, action="add_entity", name="Robert", kind="person", summary="x")
    e = store.get_entity(a["id"])
    assert e["summary"] == "a longer summary" and e["kind"] == "person"
    assert {"bob", "bobby"} <= set(e["aliases"])
    assert "error" in gr(store, action="add_entity", name="")
    assert "error" in gr(store, action="add_entity", name="n" * 201)
    assert "error" in gr(store, action="add_entity", name="n", kind="k" * 33)


def test_add_edge_create_missing_self_edge_reinforce(store):
    e = gr(store, action="add_edge", src="A", dst="B", relation="Part Of")
    assert e["created"] is True and e["weight"] == 1.0
    again = gr(store, action="add_edge", src="A", dst="B", relation="part_of")
    assert again["id"] == e["id"] and again["created"] is False and again["weight"] > 1.0
    assert "error" in gr(store, action="add_edge", src="A", dst="a", relation="r")
    assert "error" in gr(store, action="add_edge", src="A", dst="Nope", relation="r",
                         create_missing=False)
    assert "error" in gr(store, action="add_edge", src="A", dst="B", relation="bad!")
    assert "error" in gr(store, action="add_edge", src="A", dst="B", relation="r", weight=9)
    assert "error" in gr(store, action="add_edge", src="A", dst="B", relation="")
    ida = store.get_entity("A")["id"]
    ok = gr(store, action="add_edge", src=ida, dst="C", relation="r", weight=2)
    assert ok["weight"] == 2.0


# --- uninitialised / connections -----------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("KARAKOS_AGENT", "tester")
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def test_not_initialised_message_names_both_fixes():
    """A fresh install needs `lib.graph init`; a 1.x upgrade needs `karakos migrate`.
    The message must name both so an operator can tell which one applies."""
    assert "python3 -m lib.graph init" in gt.NOT_INITIALISED
    assert "karakos migrate" in gt.NOT_INITIALISED


def test_uninitialised_graph_returns_migrate_message(server, tmp_path):
    for tool, args in (("memory", {"action": "recall", "query": "x"}),
                       ("memory", {"action": "write", "content": "x"}),
                       ("graph", {"action": "add_entity", "name": "x"})):
        assert server.handle_core_tool(tool, args) == {"error": gt.NOT_INITIALISED}
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("tool,args", [
    ("memory", {"action": "write", "content": "z"}),
    ("memory", {"action": "recall", "query": "z"}),
    ("memory", {"action": "status"}),
    ("memory", {"action": "recent"}),
    ("memory", {"action": "nonsense"}),
    ("graph", {"action": "add_edge", "src": "a", "dst": "b", "relation": "r"}),
    ("graph", {"action": "add_edge", "src": "a", "dst": "a", "relation": "r"}),
])
def test_each_call_closes_its_connections(server, tmp_path, monkeypatch, tool, args):
    open_graph(tmp_path / "data", create=True)
    no_model(monkeypatch)
    opened = []
    real = sqlite3.connect

    def tracking(*a, **kw):
        opened.append(real(*a, **kw))
        return opened[-1]
    monkeypatch.setattr(sqlite3, "connect", tracking)
    server.handle_core_tool(tool, args)
    assert opened
    for c in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")


def test_server_end_to_end_agent_from_env(server, tmp_path, monkeypatch):
    open_graph(tmp_path / "data", create=True)
    no_model(monkeypatch)
    w = server.handle_core_tool("memory", {"action": "write", "content": "hello"})
    assert w["status"] == "ok"
    with sqlite3.connect(str(tmp_path / "data" / "memory" / "graph.db")) as c:
        assert c.execute("SELECT agent FROM observations").fetchone()[0] == "tester"
