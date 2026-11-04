import datetime as real
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import schedule

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import job_registry as jr  # noqa: E402


@pytest.fixture(autouse=True)
def reset():
    jr._reset()
    jr.OPTIONAL_MODULES.clear()
    yield
    jr._reset()
    jr.OPTIONAL_MODULES.clear()


def test_builtin_names_match_old_scheduler():
    names = {j.name for j in jr.load_jobs("/nonexistent")}
    for n in ("heartbeat-primary", "heartbeat-monitor", "memory-consolidate", "health-sweep",
              "wedge-check", "cli-watchdog", "flush-deferred", "purge", "update-check",
              "scheduler", "mcp-tools", "relay", "monitor-tick", "ask-poll"):
        assert n in names
    comps = {j.name for j in jr.BUILTIN if j.kind == "component"}
    assert comps == {"scheduler", "mcp-tools", "relay"}
    assert {j.name: j.max_age_s for j in jr.BUILTIN}["memory-consolidate"] == 48 * 3600


def test_hourly_marks():
    assert jr.hourly_marks(30, 15) == [15, 45]
    assert jr.hourly_marks(30, 0) == [0, 30]
    with pytest.raises(ValueError):
        jr.hourly_marks(7, 0)


def test_heartbeats_fire_at_distinct_minutes(monkeypatch):
    sched = schedule.Scheduler()
    clock = {"now": datetime(2026, 1, 1, 10, 0, 1)}

    class FakeDT(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    fake_mod = type("M", (), {k: getattr(real, k) for k in dir(real) if not k.startswith("__")})
    fake_mod.datetime = FakeDT
    monkeypatch.setattr(schedule, "datetime", fake_mod)
    fired = {"p": [], "m": []}
    by = {j.name: j for j in jr.BUILTIN}
    jr.apply_schedule(sched, by["heartbeat-primary"], lambda: fired["p"].append(clock["now"].minute))
    jr.apply_schedule(sched, by["heartbeat-monitor"], lambda: fired["m"].append(clock["now"].minute))
    for _ in range(2 * 60 * 2):  # two hours in 30 s steps
        clock["now"] += timedelta(seconds=30)
        sched.run_pending()
    assert set(fired["p"]) == {0, 30}
    assert set(fired["m"]) == {15, 45}
    assert len(fired["p"]) == 4 and len(fired["m"]) == 4


def test_non_dividing_interval_uses_seconds_and_rejects_offset():
    sched = schedule.Scheduler()
    j = jr.Job("x", "job", jr.Every(420, 0), "m:f", 1000)
    assert len(jr.apply_schedule(sched, j, lambda: None)) == 1
    with pytest.raises(ValueError):
        jr.apply_schedule(sched, jr.Job("y", "job", jr.Every(420, 60), "m:f", 1), lambda: None)


def test_jobs_yaml_disable_override_add(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jobs.yaml").write_text(
        "disable: [purge]\n"
        "override:\n  wedge-check: {max_age: 10m, every: 2m}\n"
        "jobs:\n  mine: {every: 15m, command: [echo, hi], critical: true}\n")
    jobs = {j.name: j for j in jr.load_jobs(tmp_path)}
    assert "purge" not in jobs
    assert jobs["wedge-check"].max_age_s == 600
    assert jobs["wedge-check"].schedule == jr.Every(120, 0)
    assert jobs["mine"].schedule == jr.Every(900) and jobs["mine"].critical
    assert jobs["mine"].run == ["echo", "hi"]


def test_invalid_jobs_yaml_gives_builtin_and_finding(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jobs.yaml").write_text("jobs:\n  bad: {every: 5m}\n")
    fs = []
    jobs = jr.load_jobs(tmp_path, fs)
    assert {j.name for j in jobs} == {j.name for j in jr.BUILTIN}
    assert [f.kind for f in fs] == ["jobs-config-invalid"]


def test_register_unregister_and_optional_modules(tmp_path):
    jr.register(jr.Job("consolidate", "job", jr.Daily("02:00"), "x:y", 100))
    jr.unregister("memory-consolidate")
    names = {j.name for j in jr.load_jobs(tmp_path)}
    assert "consolidate" in names and "memory-consolidate" not in names
    jr.OPTIONAL_MODULES.append("definitely_not_a_module_xyz")
    assert "consolidate" in {j.name for j in jr.load_jobs(tmp_path)}
