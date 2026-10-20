"""Persona snapshots (#241): lib behaviour and the admin tools around it."""
import sys

import pytest

from conftest import PACKAGE_ROOT, import_script

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import persona_snapshots as ps  # noqa: E402


def agent(ws, name="a", prompt="P1", voice="V1"):
    d = ws / "agents" / name
    (d / "persona").mkdir(parents=True, exist_ok=True)
    (d / "SYSTEM_PROMPT.md").write_text(prompt)
    (d / "persona" / "voice.md").write_text(voice)
    (d / "journal").mkdir(exist_ok=True)
    (d / "journal" / "j.md").write_text("private")
    return d


def test_snapshot_copies_prompt_and_persona_only(tmp_path):
    agent(tmp_path)
    r = ps.snapshot(tmp_path, "a", "first")
    snap = tmp_path / "data" / "persona-snapshots" / "a" / r["snapshot_id"]
    assert (snap / "SYSTEM_PROMPT.md").read_text() == "P1"
    assert (snap / "persona" / "voice.md").read_text() == "V1"
    assert not (snap / "journal").exists()
    assert ps.list_snapshots(tmp_path, "a")[0]["reason"] == "first"


def test_keeps_newest_ten(tmp_path):
    agent(tmp_path)
    ids = [ps.snapshot(tmp_path, "a")["snapshot_id"] for _ in range(13)]
    kept = [s["id"] for s in ps.list_snapshots(tmp_path, "a")]
    assert len(kept) == 10 and kept == sorted(ids)[-10:]


def test_restore_puts_files_back_and_is_undoable(tmp_path):
    d = agent(tmp_path)
    good = ps.snapshot(tmp_path, "a")["snapshot_id"]
    (d / "SYSTEM_PROMPT.md").write_text("BAD")
    (d / "persona" / "voice.md").write_text("BAD")
    (d / "persona" / "extra.md").write_text("BAD")
    r = ps.restore(tmp_path, "a", good)
    assert (d / "SYSTEM_PROMPT.md").read_text() == "P1"
    assert (d / "persona" / "voice.md").read_text() == "V1"
    assert not (d / "persona" / "extra.md").exists()
    assert (d / "journal" / "j.md").read_text() == "private"
    ps.restore(tmp_path, "a", r["undo_snapshot"])
    assert (d / "SYSTEM_PROMPT.md").read_text() == "BAD"


def test_restore_rejects_bad_and_missing_ids(tmp_path):
    agent(tmp_path)
    for bad in ("../x", "nope", None):
        with pytest.raises(ValueError):
            ps.restore(tmp_path, "a", bad)
    with pytest.raises(LookupError):
        ps.restore(tmp_path, "a", "20200101T000000Z")


def test_snapshot_if_changed_skips_identical_state(tmp_path):
    d = agent(tmp_path)
    assert ps.snapshot_if_changed(tmp_path, "a", "r") is not None
    assert ps.snapshot_if_changed(tmp_path, "a", "r") is None
    (d / "persona" / "voice.md").write_text("V2")
    assert ps.snapshot_if_changed(tmp_path, "a", "r") is not None
    assert ps.snapshot_if_changed(tmp_path, "ghost", "r") is None


# -- admin tools ---------------------------------------------------------------

@pytest.fixture
def admin(tmp_path, monkeypatch):
    m = import_script("admin-server", file_path=PACKAGE_ROOT / "mcp" / "admin-server.py")
    monkeypatch.setattr(m, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.delenv("KARAKOS_AGENT", raising=False)
    calls = []
    monkeypatch.setattr(m, "_http", lambda method, path, body=None: calls.append((method, path)) or {"ok": True})
    m._calls = calls
    return m


def test_reload_snapshots_only_after_a_persona_change(admin, tmp_path):
    d = agent(tmp_path)
    first = admin.tool_reload_agent({"name": "a"})
    assert "persona_snapshot" in first
    assert "persona_snapshot" not in admin.tool_reload_agent({"name": "a"})
    (d / "persona" / "voice.md").write_text("V2")
    assert "persona_snapshot" in admin.tool_reload_agent({"name": "a"})
    assert len(ps.list_snapshots(tmp_path, "a")) == 2
    assert len(admin._calls) == 3


def test_reload_still_works_without_an_agent_dir(admin):
    assert admin.tool_reload_agent({"name": "ghost"}) == {"ok": True}


def test_snapshot_and_restore_tools_and_scope(admin, tmp_path, monkeypatch):
    d = agent(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(
        "agents:\n  a: {role: builder}\n  boss: {role: primary}\n  b: {role: builder}\n")
    monkeypatch.setenv("KARAKOS_AGENT", "a")
    sid = admin.tool_snapshot_persona({"name": "a", "reason": "x"})["snapshot_id"]
    with pytest.raises(PermissionError):
        admin.tool_snapshot_persona({"name": "b"})
    with pytest.raises(PermissionError):
        admin.tool_restore_persona({"name": "b", "snapshot_id": sid})
    (d / "SYSTEM_PROMPT.md").write_text("BAD")
    admin.tool_restore_persona({"name": "a", "snapshot_id": sid})
    assert (d / "SYSTEM_PROMPT.md").read_text() == "P1"
    with pytest.raises(ValueError):
        admin.tool_restore_persona({"name": "a", "snapshot_id": "20200101T000000Z"})
    monkeypatch.setenv("KARAKOS_AGENT", "boss")
    with pytest.raises(ValueError):
        admin.tool_snapshot_persona({"name": "nobody-home"})


def test_tools_are_registered(admin):
    assert {"snapshot_persona", "restore_persona"} <= {t["name"] for t in admin.TOOLS}


def test_restoring_the_oldest_of_ten_keeps_it_and_the_agent_intact(tmp_path):
    d = agent(tmp_path)
    oldest = ps.snapshot(tmp_path, "a", "oldest")["snapshot_id"]
    for _ in range(9):
        ps.snapshot(tmp_path, "a")
    assert len(ps.list_snapshots(tmp_path, "a")) == 10
    (d / "SYSTEM_PROMPT.md").write_text("BAD")
    (d / "persona" / "voice.md").write_text("BAD")
    r = ps.restore(tmp_path, "a", oldest)
    assert (d / "SYSTEM_PROMPT.md").read_text() == "P1"
    assert (d / "persona" / "voice.md").read_text() == "V1"
    ids = [s["id"] for s in ps.list_snapshots(tmp_path, "a")]
    assert oldest in ids and r["undo_snapshot"] in ids
