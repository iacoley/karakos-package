"""Build-time calibration (#242)."""
import json
import sqlite3
import subprocess
import sys

import pytest

from conftest import PACKAGE_ROOT
from lib import buildq, estimate

SCRIPT = PACKAGE_ROOT / "bin" / "estimate.py"


def make_db(path, rows):
    conn = sqlite3.connect(path)
    buildq.init_schema(conn)
    for i, (kind, status, start, end) in enumerate(rows):
        conn.execute("INSERT INTO build_queue(id, kind, status, brief, created_at, started_at, "
                     "finished_at) VALUES (?,?,?,?,?,?,?)",
                     (f"q{i}", kind, status, "b", start, start, end))
    conn.commit()
    conn.close()


def mins(n):
    return f"2026-10-01T10:{n:02d}:00+00:00"


def done(kind, minutes):
    return (kind, "done", "2026-10-01T10:00:00+00:00", mins(minutes))


def test_durations_use_only_finished_done_builds(tmp_path):
    db = tmp_path / "q.db"
    make_db(db, [done("build", 4), ("build", "failed", mins(0), mins(9)),
                 ("build", "running", mins(0), None), done("review", 2)])
    assert estimate.durations(db) == {"build": [4.0], "review": [2.0]}


def test_missing_database_or_table_is_empty(tmp_path):
    assert estimate.durations(tmp_path / "nope.db") == {}
    empty = tmp_path / "e.db"
    sqlite3.connect(empty).close()
    assert estimate.durations(empty) == {}


def test_calibrate_median_p80_and_thin_data(tmp_path):
    db = tmp_path / "q.db"
    make_db(db, [done("build", m) for m in (2, 4, 6, 8, 20)] + [done("review", 3), done("review", 5)])
    cal = estimate.calibrate(db, now=1_790_000_000)
    b = cal["kinds"]["build"]
    assert b == {"n": 5, "enough": True, "median_min": 6.0, "p80_min": 8.0}
    assert cal["kinds"]["review"] == {"n": 2, "enough": False}


def test_estimate_for_gives_a_range_or_refuses():
    cal = {"min_samples": 3, "kinds": {"build": {"n": 5, "enough": True, "median_min": 4.0, "p80_min": 7.0},
                                       "review": {"n": 1, "enough": False}}}
    r = estimate.estimate_for(cal, "build", 3)
    assert r["quotable"] and (r["low_min"], r["high_min"]) == (12.0, 21.0)
    assert not estimate.estimate_for(cal, "review")["quotable"]
    assert not estimate.estimate_for(cal, "other")["quotable"]
    with pytest.raises(ValueError):
        estimate.estimate_for(cal, "build", 0)


def run(ws, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(ws), *args],
                          capture_output=True, text=True)


def test_cli_calibrate_writes_the_file_and_dry_run_does_not(tmp_path):
    (tmp_path / "data").mkdir()
    make_db(tmp_path / "data" / "build-queue.db", [done("build", m) for m in (2, 4, 6)])
    out = tmp_path / "data" / "estimates" / "calibration.json"
    assert run(tmp_path, "calibrate", "--dry-run").returncode == 0 and not out.exists()
    assert run(tmp_path, "calibrate").returncode == 0
    assert json.loads(out.read_text())["kinds"]["build"]["median_min"] == 4.0


def test_cli_for_exit_codes(tmp_path):
    (tmp_path / "data").mkdir()
    make_db(tmp_path / "data" / "build-queue.db", [done("build", m) for m in (2, 4, 6)])
    ok = run(tmp_path, "for", "build", "--builds", "2")
    assert ok.returncode == 0 and "8.0 to" in ok.stdout
    no = run(tmp_path, "for", "review")
    assert no.returncode == 1 and "no estimate" in no.stdout
    assert run(tmp_path, "for", "build", "--builds", "0").returncode == 2


def test_cli_with_no_database_says_so(tmp_path):
    r = run(tmp_path, "show")
    assert r.returncode == 0 and "no finished builds" in r.stdout
