"""bin/self-diagnose.py: read-only whole-install diagnosis."""
import json
import os

import pytest

from conftest import import_script

sd = import_script("self-diagnose")


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    (tmp_path / "data" / "health" / "heartbeats").mkdir(parents=True)
    (tmp_path / "data" / ".schema-version").write_text(json.dumps({"schema": 2}))
    (tmp_path / "logs").mkdir()
    return tmp_path


def run(ws, sections):
    return sd.diagnose(str(ws), sections)


def test_clean_install_has_no_findings(ws):
    assert run(ws, ["findings", "heartbeats", "hook", "schema", "logs"]) == [
        {"section": "findings", "severity": "info", "why": "no monitor findings file yet"}]


def test_missing_hook_and_schema_warn(ws):
    (ws / ".git" / "hooks" / "pre-commit").unlink()
    (ws / "data" / ".schema-version").unlink()
    got = {f["section"] for f in run(ws, ["hook", "schema"]) if f["severity"] == "warn"}
    assert got == {"hook", "schema"}


def test_hook_not_executable_warns(ws):
    (ws / ".git" / "hooks" / "pre-commit").chmod(0o644)
    assert "not executable" in run(ws, ["hook"])[0]["why"]


def test_monitor_findings_keep_their_severity(ws):
    (ws / "data" / "health" / "findings.json").write_text(json.dumps({"findings": [
        {"kind": "outbox", "subject": "q", "severity": "critical", "why": "stuck"}]}))
    f = run(ws, ["findings"])
    assert f == [{"section": "findings", "severity": "critical", "why": "outbox:q: stuck"}]


def test_failed_and_corrupt_heartbeats_warn(ws):
    hb = ws / "data" / "health" / "heartbeats"
    (hb / "a.json").write_text(json.dumps({"ok": False, "detail": "boom"}))
    (hb / "b.json").write_text("{nope")
    (hb / "c.json").write_text(json.dumps({"ok": True}))
    why = " ".join(f["why"] for f in run(ws, ["heartbeats"]))
    assert "a: last run failed (boom)" in why and "b: heartbeat file is unreadable" in why
    assert "c:" not in why


def test_health_unreachable_is_critical(ws, monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_URL", "http://127.0.0.1:9")
    assert run(ws, ["health"])[0]["severity"] == "critical"


def test_health_findings_from_payload(ws):
    h = {"status": "healthy", "agents": {"a": {"alive": False}}, "dead_letters": 2,
         "outbox": {"dead": 1, "oldest_pending_age_s": 900}}
    got = sd.check_health(str(ws), fetch=lambda: h)
    assert [g["severity"] for g in got] == ["critical", "warn", "warn", "warn"]
    assert sd.check_health(str(ws), fetch=lambda: {"status": "healthy", "agents": {}}) == []


def test_disk_thresholds(ws, monkeypatch):
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(sd.shutil, "disk_usage", lambda p: U(100, 91, 9))
    assert run(ws, ["disk"])[0]["severity"] == "warn"
    monkeypatch.setattr(sd.shutil, "disk_usage", lambda p: U(100, 98, 2))
    assert run(ws, ["disk"])[0]["severity"] == "critical"
    monkeypatch.setattr(sd.shutil, "disk_usage", lambda p: U(100, 10, 90))
    assert run(ws, ["disk"]) == []


def test_log_errors_counted_from_the_tail(ws):
    (ws / "logs" / "relay.log").write_text("ok\n" + "x ERROR bad\n" * 6)
    (ws / "logs" / "hook-events.log").write_text("ERROR\n" * 50)
    got = run(ws, ["logs"])
    assert len(got) == 1 and "relay.log: 6 error lines" in got[0]["why"]


def test_ranked_worst_first_and_exit_codes(ws, capsys):
    (ws / ".git" / "hooks" / "pre-commit").unlink()
    (ws / "data" / "health" / "findings.json").write_text(json.dumps({"findings": [
        {"kind": "k", "subject": "s", "severity": "critical", "why": "w"}]}))
    assert sd.main(["--workspace", str(ws), "--sections", "hook,findings", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert [f["severity"] for f in out["findings"]] == ["critical", "warn"]
    assert sd.main(["--workspace", str(ws), "--sections", "schema"]) == 0


def test_unknown_section_is_rejected(ws):
    with pytest.raises(SystemExit):
        sd.main(["--workspace", str(ws), "--sections", "bogus"])


# -- build queue and version drift (#236 part 3) ------------------------------

def _queue(ws, rows, enabled=True):
    import sqlite3
    (ws / "config").mkdir(exist_ok=True)
    (ws / "config" / "build-queue.yaml").write_text(f"enabled: {'true' if enabled else 'false'}\n")
    c = sqlite3.connect(ws / "data" / "build-queue.db")
    c.execute("CREATE TABLE IF NOT EXISTS build_queue (id TEXT, status TEXT, created_at TEXT)")
    c.execute("DELETE FROM build_queue")
    c.executemany("INSERT INTO build_queue VALUES (?,?,?)", rows)
    c.commit()
    c.close()


def test_buildqueue_off_or_empty_is_quiet(ws):
    assert run(ws, ["buildqueue"]) == []
    _queue(ws, [], enabled=False)
    assert run(ws, ["buildqueue"]) == []
    _queue(ws, [("a", "done", "2020-01-01T00:00:00Z")])
    assert run(ws, ["buildqueue"]) == []


def test_buildqueue_stuck_backlog_warns_running_one_is_info(ws):
    _queue(ws, [("a", "queued", "2020-01-01T00:00:00Z")])
    [f] = run(ws, ["buildqueue"])
    assert f["severity"] == "warn" and "none running" in f["why"]
    _queue(ws, [("a", "queued", "2020-01-01T00:00:00Z"), ("b", "running", "2020-01-01T00:00:00Z")])
    assert run(ws, ["buildqueue"])[0]["severity"] == "info"


def test_buildqueue_enabled_without_database_warns(ws):
    (ws / "config").mkdir()
    (ws / "config" / "build-queue.yaml").write_text("enabled: true\n")
    assert run(ws, ["buildqueue"])[0]["severity"] == "warn"


def test_version_drift_is_info_only_when_they_differ(ws):
    assert run(ws, ["version"])[0]["why"].startswith("no VERSION")
    (ws / "VERSION").write_text("2.2.0\n")
    (ws / "data" / ".schema-version").write_text(json.dumps({"schema": 2, "package": "2.2.0"}))
    assert run(ws, ["version"]) == []
    (ws / "data" / ".schema-version").write_text(json.dumps({"schema": 2, "package": "2.0.0"}))
    [f] = run(ws, ["version"])
    assert f["severity"] == "info" and "2.0.0" in f["why"]
