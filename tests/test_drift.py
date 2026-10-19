import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from drift import drift_report  # noqa: E402
from job_registry import Job, Every, Daily  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1)
J = Job("j", "job", Every(60), "m:f", 300)
D = Job("d", "job", Daily("03:00"), "m:f", 48 * 3600)
C = Job("c", "component", None, None, 300, critical=True)


def ts(sec_ago):
    return (NOW - timedelta(seconds=sec_ago)).isoformat()


def keys(fs):
    return sorted(f.key for f in fs)


def live(*names):
    return {"started_at": START.isoformat(), "jobs": [{"name": n} for n in names]}


def test_clean():
    hbs = {"j": {"timestamp": ts(10), "ok": True, "previous_timestamp": ts(70)}, "c": {"timestamp": ts(5)}}
    assert drift_report([J, C], live("j"), hbs, START, NOW) == []


def test_not_scheduled_and_unknown():
    hbs = {"j": {"timestamp": ts(10), "ok": True}}
    fs = drift_report([J], live("ghost"), hbs, START, NOW)
    assert keys(fs) == ["job-not-scheduled:j", "job-unknown:ghost"]
    assert [f.severity for f in fs if f.kind == "job-unknown"] == ["info"]


def test_stale_failing_late():
    assert keys(drift_report([J], live("j"), {"j": {"timestamp": ts(900), "ok": True}}, START, NOW)) == ["job-stale:j"]
    assert keys(drift_report([J], live("j"), {"j": {"timestamp": ts(10), "ok": False, "detail": "x"}}, START, NOW)) == ["job-failing:j"]
    late = {"j": {"timestamp": ts(10), "ok": True, "previous_timestamp": ts(10 + 121)}}
    assert keys(drift_report([J], live("j"), late, START, NOW)) == ["job-late:j"]


def test_daily_not_late():
    hbs = {"d": {"timestamp": ts(10), "ok": True, "previous_timestamp": ts(10 + 86400)}}
    assert drift_report([D], live("d"), hbs, START, NOW) == []


def test_components():
    stale = drift_report([C], None, {"c": {"timestamp": ts(900)}}, START, NOW)
    assert keys(stale) == ["component-stale:c"] and stale[0].severity == "critical"
    assert keys(drift_report([C], None, {"c": None}, START, NOW)) == ["component-missing:c"]
    # inside boot grace a missing component is not reported
    assert drift_report([C], None, {"c": None}, NOW - timedelta(seconds=30), NOW) == []


def test_no_job_late_when_scheduler_restarted_between_runs():
    # previous run (now-200s) predates the restart (now-100s): the gap is a restart, not lateness
    hbs = {"j": {"timestamp": ts(0), "ok": True, "previous_timestamp": ts(200)}}
    assert drift_report([J], live("j"), hbs, NOW - timedelta(seconds=100), NOW) == []


def test_job_late_when_previous_run_is_after_scheduler_start():
    # previous run (now-200s) is after the scheduler started (now-1000s): a real late run
    hbs = {"j": {"timestamp": ts(0), "ok": True, "previous_timestamp": ts(200)}}
    assert keys(drift_report([J], live("j"), hbs, NOW - timedelta(seconds=1000), NOW)) == ["job-late:j"]
