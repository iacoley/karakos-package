"""ITHILIEN phase 1: the capability manifest, its checks, and bin/agent-check.py.

Every check runs against temp directories. The MCP and ask checks start their own
processes or a local stub, and no test here reaches a live agent server or Discord.
"""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from conftest import PACKAGE_ROOT, import_script

MANIFEST = PACKAGE_ROOT / "tests" / "capabilities" / "manifest.yaml"
EXPECTED = PACKAGE_ROOT / "tests" / "capabilities" / "expected_tools.json"
RUNNER = PACKAGE_ROOT / "bin" / "agent-check.py"


@pytest.fixture(scope="module")
def runner():
    return import_script("agent-check", RUNNER)


@pytest.fixture(scope="module")
def checks(runner):
    return runner.agent_checks


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A temp install root. Workspace env points here so runner output stays in tmp."""
    monkeypatch.setenv("KARAKOS_HOME", str(tmp_path))
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    return tmp_path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _entries(runner):
    return runner.load_manifest(MANIFEST)


def test_manifest_parses_and_ids_are_unique(runner):
    entries = _entries(runner)
    assert len(entries) >= 11
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    for e in entries:
        for key in ("id", "layer", "desc", "since", "check"):
            assert e.get(key), f"{e.get('id')} lacks {key}"
        assert e["layer"] in ("programmatic", "agentic")


def test_every_check_names_a_real_function(runner, checks):
    for e in _entries(runner):
        fn = getattr(checks, e["check"], None)
        assert callable(fn), f"{e['id']}: {e['check']} is not a function in lib/agent_checks.py"


def test_every_expected_tool_has_a_manifest_entry(runner):
    """Coverage is by `covers:` on an entry, so the tool list lives in one place."""
    expected = {t["name"] for t in json.loads(EXPECTED.read_text())["tools"]}
    covered = [name for e in _entries(runner) for name in e.get("covers", [])]
    assert set(covered) == expected
    assert len(covered) == len(set(covered)), "a tool is covered by two entries"


def test_run_one_reports_agentic_as_skip_and_bad_check_as_fail(runner, ws):
    skip = runner.run_one({"id": "later", "layer": "agentic", "check": "none",
                           "desc": "", "since": "x"}, ws)
    assert skip["status"] == "SKIP" and skip["detail"] == "skipped: phase 2"
    bad = runner.run_one({"id": "typo", "layer": "programmatic", "check": "no_such_check",
                          "desc": "", "since": "x"}, ws)
    assert bad["status"] == "FAIL" and "no_such_check" in bad["detail"]


def test_clean_temp_install_exits_zero_with_documented_keys(runner, ws):
    env = {**os.environ, "KARAKOS_HOME": str(ws), "WORKSPACE_ROOT": str(ws),
           "AGENT_SERVER_URL": f"http://127.0.0.1:{_free_port()}", "AGENT_SERVER_TOKEN": ""}
    p = subprocess.run([sys.executable, str(RUNNER), "--json"], env=env, cwd=str(ws),
                       capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stdout + p.stderr
    doc = json.loads(p.stdout)
    assert set(doc) == {"version", "ran_at", "results"}
    assert doc["version"] == (PACKAGE_ROOT / "VERSION").read_text().strip()
    assert doc["ran_at"].endswith("Z")
    ids = {r["id"] for r in doc["results"]}
    assert ids == {e["id"] for e in _entries(runner)}
    for r in doc["results"]:
        assert set(r) == {"id", "layer", "status", "detail", "ms"}
        assert r["status"] in ("PASS", "FAIL", "SKIP")
    session = next(r for r in doc["results"] if r["id"] == "session-finalize-endpoint")
    assert session["status"] == "PASS" and session["detail"] == "no live server; skipped"
    lines = (ws / "data" / "agent-checks" / "results.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["results"] == doc["results"]


def test_failing_check_gives_exit_1(runner, ws, monkeypatch, capsys):
    monkeypatch.setattr(runner.agent_checks, "outbox_roundtrip",
                        lambda workspace: (False, "forced failure"))
    rc = runner.main(["--only", "outbox-roundtrip"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("FAIL") and "forced failure" in out


def test_passing_subset_exits_zero_and_unknown_id_fails(runner, ws, capsys):
    assert runner.main(["--only", "embed-parse,schema-stamp"]) == 0
    assert runner.main(["--only", "no-such-capability"]) == 1
    assert "no such capability" in capsys.readouterr().out


def test_checks_never_raise(checks, ws, monkeypatch):
    def broken_table(workspace):
        raise RuntimeError("table gone")
    monkeypatch.setattr(checks.job_registry, "load_jobs", broken_table)
    ok, detail = checks.health_files_fresh(ws)  # no health dir yet: returns before the table
    assert ok and detail == "no health dir"
    (ws / "data" / "health").mkdir(parents=True)
    (ws / "data" / "health" / "scheduler.json").write_text("{}")
    ok, detail = checks.health_files_fresh(ws)
    assert not ok and "RuntimeError: table gone" in detail


def test_schema_stamp_mismatch_fails(checks, ws):
    (ws / "data").mkdir()
    (ws / "data" / ".schema-version").write_text(json.dumps({"schema": 999}))
    ok, detail = checks.schema_stamp(ws)
    assert not ok and "999" in detail


def test_schema_stamp_unstamped_nonempty_fails_empty_passes(checks, ws):
    assert checks.schema_stamp(ws)[0] is True
    (ws / "data").mkdir()
    (ws / "data" / "outbox.db").write_text("x")
    assert checks.schema_stamp(ws)[0] is False


def test_health_files_stale_fails(checks, ws):
    h = ws / "data" / "health"
    h.mkdir(parents=True)
    f = h / "scheduler.json"
    f.write_text("{}")
    old = time.time() - 10 * 86400  # scheduler's threshold is 300 s
    os.utime(f, (old, old))
    ok, detail = checks.health_files_fresh(ws)
    assert not ok and "scheduler.json" in detail


def test_roundtrip_checks_pass_in_temp_dirs(checks, ws):
    assert checks.outbox_roundtrip(ws)[0]
    assert checks.memory_roundtrip(ws)[0]
    assert checks.embed_parse(ws)[0]
    assert checks.ask_roundtrip(ws)[0]
    # Nothing a roundtrip writes lands in the install.
    assert not (ws / "data" / "outbox").exists()
    assert not (ws / "data" / "asks").exists()


def test_hooks_installed_matches_the_checkout(checks, ws):
    ok, detail = checks.hooks_installed(ws)
    assert ok, detail


def test_scheduler_jobs_registers_every_job(checks, ws):
    ok, detail = checks.scheduler_jobs(ws)
    assert ok, detail
    assert "ask-poll" in detail


def test_session_finalize_skips_without_a_server(checks, ws, monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_URL", f"http://127.0.0.1:{_free_port()}")
    assert checks.session_finalize_endpoint(ws) == (True, "no live server; skipped")
