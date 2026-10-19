"""Drift: compare the job table against the live schedule and the heartbeats. Pure."""
from __future__ import annotations

import heartbeats
from findings import make
from job_registry import Every


def drift_report(jobs, live, hbs, started_at, now) -> list:
    """`live`: parsed scheduler-jobs.json or None; `hbs`: name -> record (see
    heartbeats.read_all); `started_at`: aware datetime or None."""
    out = []
    live_names = {j.get("name") for j in (live or {}).get("jobs", [])} if live is not None else None
    since = now.isoformat()
    known = {j.name for j in jobs}
    for job in jobs:
        hb = hbs.get(job.name)
        st = heartbeats.status(job, hb, started_at, now)
        sev = "critical" if job.critical else "warn"
        if job.kind == "component":
            if st == "stale" and hb is None:
                out.append(make("component-missing", job.name, sev,
                                f"component {job.name} has no health file", since))
            elif st == "stale":
                out.append(make("component-stale", job.name, sev,
                                f"component {job.name} has not reported within {job.max_age_s}s", since))
            continue
        if live_names is not None and job.name not in live_names:
            out.append(make("job-not-scheduled", job.name, "warn",
                            f"job {job.name} is in the table but not in the live schedule", since))
        if st == "stale":
            out.append(make("job-stale", job.name, sev,
                            f"job {job.name} has not run within {job.max_age_s}s", since))
        elif st == "failing":
            out.append(make("job-failing", job.name, "warn",
                            f"job {job.name} last run failed: {(hb or {}).get('detail', '')}", since))
        if isinstance(job.schedule, Every) and hb and not hb.get("corrupt"):
            a, b = heartbeats.parse_ts(hb.get("timestamp")), heartbeats.parse_ts(hb.get("previous_timestamp"))
            restarted_between = started_at is not None and b is not None and b < started_at
            if a and b and not restarted_between and (a - b).total_seconds() > 2 * job.schedule.seconds:
                out.append(make("job-late", job.name, "warn",
                                f"job {job.name} last two runs were {(a - b).total_seconds():.0f}s apart "
                                f"(interval {job.schedule.seconds}s)", since))
    for name in sorted((live_names or set()) - known):
        out.append(make("job-unknown", name, "info", f"live schedule names unknown job {name}", since))
    return out
