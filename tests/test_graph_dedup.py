"""Entity dedup (#238 build B): candidates, dry run, live cap, CLI, weekly pass."""
import json
import sys
from datetime import datetime, timezone

import pytest

from lib.graph import embed, entity_dedup
from lib.graph.store import open_graph
from tests.graph.helpers import _fresh_embedder  # noqa: F401  (autouse fixture)
from tests.graph.helpers import blob
from conftest import import_script


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    return open_graph(tmp_path / "data", create=True)


def ent(store, name, vec, kind="person", obs=0):
    eid = store.add_entity(name, kind)[0]
    store.set_embedding("entities", eid, blob(*vec))
    for i in range(obs):
        store.add_observation(f"{name} fact {i}", entity=name)
    return eid


def archived(store, eid):
    with store.read() as c:
        return c.execute("SELECT archived_at FROM entities WHERE id=?", (eid,)).fetchone()[0]


def test_near_duplicates_pair_up_and_the_busier_one_survives(store):
    a = ent(store, "Ian Coley", (1, 0), obs=3)
    b = ent(store, "ian c.", (1, 0.05), obs=1)
    ent(store, "Someone Else", (0, 1))
    [p] = entity_dedup.candidates(store)
    assert (p["from"], p["into"]) == (b, a) and p["score"] > 0.99


def test_different_kinds_never_pair_and_below_threshold_is_ignored(store):
    ent(store, "Mercury", (1, 0), kind="planet")
    ent(store, "Mercury Inc", (1, 0), kind="company")
    ent(store, "Far", (1, 1))
    assert entity_dedup.candidates(store) == []


def test_each_entity_appears_in_one_pair(store):
    for n in ("Aria", "Aria B", "Aria C"):
        ent(store, n, (1, 0))
    assert len(entity_dedup.candidates(store)) == 1


def test_dry_run_changes_nothing_live_merges_softly(store):
    a = ent(store, "Alpha", (1, 0), obs=2)
    b = ent(store, "Alpha Beta", (1, 0))
    dry = entity_dedup.run(store)
    assert dry["candidates"] == 1 and dry["merged"] == 0 and archived(store, b) is None
    live = entity_dedup.run(store, live=True)
    assert live["merged"] == 1 and archived(store, b) is not None and archived(store, a) is None
    with store.read() as c:
        assert c.execute("SELECT agent, action FROM memory_audit").fetchone()[0] == "graph-dedup"


def test_live_run_is_capped(store, monkeypatch):
    for i in range(4):
        ent(store, f"x{i} a", (1, i * 0.0), kind=f"k{i}")
        ent(store, f"x{i} b", (1, 0), kind=f"k{i}")
    r = entity_dedup.run(store, live=True, max_merges=2)
    assert r["merged"] == 2 and r["capped"] is True and r["candidates"] == 4
    assert entity_dedup.run(store, live=True, max_merges=10_000)["merged"] == 2  # the rest, never above MAX
    assert entity_dedup.MAX_MERGES == 50


def test_entities_without_current_model_embeddings_are_skipped(store):
    ent(store, "One", (1, 0))
    eid = ent(store, "Two", (1, 0))
    with store.write() as c:
        c.execute("UPDATE entities SET embed_model='other' WHERE id=?", (eid,))
    assert entity_dedup.candidates(store) == []


def test_cli_dry_run_then_live(store, tmp_path, capsys):
    ent(store, "Gamma", (1, 0), obs=1)
    ent(store, "Gamma Two", (1, 0))
    cli = import_script("graph-dedup")
    assert cli.main(["--workspace", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["merged"] == 0
    assert cli.main(["--workspace", str(tmp_path), "--live", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["merged"] == 1
    assert cli.main(["--workspace", str(tmp_path / "nowhere")]) == 1
    with pytest.raises(SystemExit):
        cli.main(["--workspace", str(tmp_path), "--threshold", "0.1"])


def test_weekly_pass_is_off_by_default_and_runs_on_sunday(store, monkeypatch):
    from lib.monitor_jobs import memory_consolidate as mc
    ent(store, "Delta", (1, 0))
    ent(store, "Delta Prime", (1, 0))
    sunday, monday = datetime(2026, 10, 18, tzinfo=timezone.utc), datetime(2026, 10, 19, tzinfo=timezone.utc)
    stats = {}
    mc._entity_dedup_pass(store, stats, sunday)
    assert stats == {}
    monkeypatch.setenv("MEMORY_ENTITY_DEDUP", "report")
    mc._entity_dedup_pass(store, stats, monday)
    assert stats == {}
    mc._entity_dedup_pass(store, stats, sunday)
    assert stats["entity_dedup"]["candidates"] == 1 and stats["entity_dedup"]["merged"] == 0
    monkeypatch.setenv("MEMORY_ENTITY_DEDUP", "live")
    mc._entity_dedup_pass(store, stats, sunday)
    assert stats["entity_dedup"]["merged"] == 1


def test_numbers_that_differ_never_merge_even_at_high_cosine(store):
    # measured on bge-small: these score 0.94 on bare names
    ent(store, "2026-10-19", (1, 0), kind="date")
    ent(store, "2026-10-21", (1, 0.01), kind="date")
    ent(store, "v2.2.0", (1, 0), kind="version")
    ent(store, "v2.3.1", (1, 0.01), kind="version")
    assert entity_dedup.candidates(store) == []


def test_identical_numbers_still_pair(store):
    ent(store, "release v2.3.1", (1, 0), kind="version")
    ent(store, "Release v2.3.1 notes", (1, 0.01), kind="version")
    assert len(entity_dedup.candidates(store)) == 1


def test_unrelated_names_with_high_cosine_do_not_pair(store):
    ent(store, "Mercury", (1, 0))
    ent(store, "Quokka", (1, 0.01))
    assert entity_dedup.candidates(store) == []


def test_abbreviation_pairs_below_the_main_threshold_but_not_far_below(store):
    ent(store, "Ian Coley", (1, 0))
    ent(store, "ian c.", (0.72, 0.694))   # cosine 0.72, as measured
    assert len(entity_dedup.candidates(store)) == 1
    ent(store, "Dana Reyes", (1, 0), kind="other")
    ent(store, "dana r.", (0.3, 0.954), kind="other")   # cosine 0.3: too far
    assert len(entity_dedup.candidates(store)) == 1


def test_same_thing_rules():
    assert entity_dedup.same_thing("Ian Coley", "ian c.", 0.72)
    assert not entity_dedup.same_thing("Ian Coley", "Bob Smith", 0.72)
    assert not entity_dedup.same_thing("", "x", 0.99)


def test_scoring_in_chunks_finds_the_same_pairs(store, monkeypatch):
    for n in ("Echo", "Echo Two", "Foxtrot", "Foxtrot Two", "Golf", "Golf Two"):
        ent(store, n, (1, 0) if n.startswith("Echo") else (0, 1) if n.startswith("Fox") else (1, 1))
    whole = entity_dedup.candidates(store)
    monkeypatch.setattr(entity_dedup, "CHUNK", 2)
    assert entity_dedup.candidates(store) == whole and len(whole) == 3


def test_weekly_pass_honours_dry_run(store, monkeypatch):
    from lib.monitor_jobs import memory_consolidate as mc
    ent(store, "Hotel", (1, 0))
    b = ent(store, "Hotel Two", (1, 0))
    monkeypatch.setenv("MEMORY_ENTITY_DEDUP", "live")
    stats = {}
    mc._entity_dedup_pass(store, stats, datetime(2026, 10, 18, tzinfo=timezone.utc), dry_run=True)
    assert stats["entity_dedup"]["live"] is False and stats["entity_dedup"]["merged"] == 0
    assert archived(store, b) is None
