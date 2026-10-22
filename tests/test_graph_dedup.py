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
    a = ent(store, "Ian Hartley", (1, 0), obs=3)
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
    a = ent(store, "Alpha Beta", (1, 0), obs=2)
    b = ent(store, "Alpha Betta", (1, 0))
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
    ent(store, "Gamma Ray", (1, 0), obs=1)
    ent(store, "Gamma Rays", (1, 0))
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
    ent(store, "Delta Prime", (1, 0))
    ent(store, "Delta Primes", (1, 0))
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
    ent(store, "release notes v2.3.1", (1, 0), kind="version")
    ent(store, "Release note v2.3.1", (1, 0.01), kind="version")
    assert len(entity_dedup.candidates(store)) == 1


def test_unrelated_names_with_high_cosine_do_not_pair(store):
    ent(store, "Mercury", (1, 0))
    ent(store, "Quokka", (1, 0.01))
    assert entity_dedup.candidates(store) == []


def test_abbreviation_pairs_below_the_main_threshold_but_not_far_below(store):
    ent(store, "Ian Hartley", (1, 0))
    ent(store, "ian c.", (0.72, 0.694))   # cosine 0.72, as measured
    assert len(entity_dedup.candidates(store)) == 1
    ent(store, "Dana Reyes", (1, 0), kind="other")
    ent(store, "dana r.", (0.3, 0.954), kind="other")   # cosine 0.3: too far
    assert len(entity_dedup.candidates(store)) == 1


def test_verdict_rules():
    v = entity_dedup.verdict
    assert v("Ian Hartley", "ian c.", 0.72) == "abbrev"
    assert v("Ian Hartley", "Bob Smith", 0.72) is None
    assert v("", "x", 0.99) is None
    assert v("Delta Prime", "Delta Primes", 0.95) == "merge"


# Pairs Amos measured with the real bge-small: none may merge.
@pytest.mark.parametrize("a,b,cos", [
    ("karakos", "karakos-package", 0.862), ("Marvin", "Marvin harness", 0.850),
    ("Mike", "Mike Tyson", 0.721), ("Alpha", "Alpha Beta", 0.95)])
def test_fewer_words_is_reported_never_merged(store, a, b, cos):
    assert entity_dedup.verdict(a, b, cos) == "report"
    x = ent(store, a, (1, 0), obs=1)
    y = ent(store, b, (cos, (1 - cos * cos) ** 0.5))
    assert entity_dedup.candidates(store) == []
    [r] = entity_dedup.report_only(store)
    assert {r["a"], r["b"]} == {x, y} and r["why"] == "report"
    assert entity_dedup.run(store, live=True)["merged"] == 0
    assert archived(store, x) is None and archived(store, y) is None


def test_an_abbreviation_that_fits_two_entities_merges_neither(store):
    a = ent(store, "ian c.", (1, 0))
    b = ent(store, "Ian Cooper", (0.795, 0.606))   # cosine 0.795 to "ian c.", as measured
    d = ent(store, "Ian Hartley", (0.795, -0.606))   # the two Ians stay far apart
    assert entity_dedup.candidates(store) == []
    assert {r["why"] for r in entity_dedup.report_only(store)} == {"ambiguous"}
    entity_dedup.run(store, live=True)
    assert all(archived(store, i) is None for i in (a, b, d))


def test_cli_lists_review_only_pairs(store, tmp_path, capsys):
    ent(store, "Mike", (1, 0))
    ent(store, "Mike Tyson", (0.8, 0.6))
    cli = import_script("graph-dedup")
    assert cli.main(["--workspace", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "review only (report)" in out and "0 merged" in out


def test_scoring_in_chunks_finds_the_same_pairs(store, monkeypatch):
    for n in ("Echo Two", "Echo Twos", "Foxtrot Two", "Foxtrot Twos", "Golf Two", "Golf Twos"):
        ent(store, n, (1, 0) if n.startswith("Echo") else (0, 1) if n.startswith("Fox") else (1, 1))
    whole = entity_dedup.candidates(store)
    monkeypatch.setattr(entity_dedup, "CHUNK", 2)
    assert entity_dedup.candidates(store) == whole and len(whole) == 3


def test_weekly_pass_honours_dry_run(store, monkeypatch):
    from lib.monitor_jobs import memory_consolidate as mc
    ent(store, "Hotel Two", (1, 0))
    b = ent(store, "Hotel Twos", (1, 0))
    monkeypatch.setenv("MEMORY_ENTITY_DEDUP", "live")
    stats = {}
    mc._entity_dedup_pass(store, stats, datetime(2026, 10, 18, tzinfo=timezone.utc), dry_run=True)
    assert stats["entity_dedup"]["live"] is False and stats["entity_dedup"]["merged"] == 0
    assert archived(store, b) is None


# Prefix pairs Amos measured with the real bge-small: longer prefixes are not abbreviations.
@pytest.mark.parametrize("a,b,cos", [
    ("git", "github", 0.899), ("Ian Cole", "Ian Hartley", 0.910), ("Marv", "Marvin", 0.720)])
def test_long_prefixes_are_not_abbreviations(a, b, cos):
    assert entity_dedup.verdict(a, b, cos) is None


def test_abbreviation_needs_an_initial_and_an_exact_word():
    v = entity_dedup.verdict
    assert v("Ian Hartley", "i hartley", 0.7) == "abbrev"
    assert v("Ian Hartley", "ia c", 0.7) is None        # no exact word
    assert v("Ian Hartley", "ian col", 0.7) is None     # three letters is not an initial


@pytest.mark.slow
def test_real_model_keeps_prefix_pairs_apart(store, monkeypatch):
    """Runs the real embedding model; skipped when it cannot load offline."""
    monkeypatch.undo()
    embed._reset()
    fe = pytest.importorskip("fastembed")
    try:
        model = fe.TextEmbedding("BAAI/bge-small-en-v1.5")
    except Exception as exc:  # no cached weights, no network
        pytest.skip(f"real model unavailable: {exc}")
    import numpy as np
    pairs = [("git", "github"), ("Ian Cole", "Ian Hartley"), ("Marv", "Marvin")]
    for a, b in pairs:
        va, vb = (np.asarray(x) for x in model.embed([a, b]))
        cos = float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb)))
        assert entity_dedup.verdict(a, b, cos) in (None, "report"), (a, b, cos)
