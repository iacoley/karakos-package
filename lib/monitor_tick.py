"""The monitor's once-a-minute pass: drift -> findings -> alerts.

`tick` is the only caller of `alerts.send` besides health-monitor's daily run.
Its heartbeat is touched by the scheduler wrapper (touching it here too would
make `previous_timestamp` equal to the current run and blind job-late).
"""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

import alerts
import drift
import findings as findings_mod
import heartbeats
import job_registry
import monitor_config
import outbox_audit
import procinfo
import procreap
import stall
from findings import make

# Later steps append (name, fn(workspace, now, config) -> list[Finding]) here.
EXTRA_STAGES: list = []

SLOW_EVERY = 5      # the outbox audit and the orphan sweep run every fifth tick
SLOW_KINDS = ("outbox-dead", "outbox-stuck", "inbound-deferred", "inbound-invalid",
              "orphan-process")


def _health(workspace) -> Path:
    return Path(workspace) / "data" / "health"


def _load_json(path: Path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _next_count(workspace) -> int:
    """Tick number before this one (0 on the first tick); persisted."""
    path = _health(workspace) / "monitor-tick.json"
    n = (_load_json(path) or {}).get("n", 0)
    n = n if isinstance(n, int) and n >= 0 else 0
    try:
        findings_mod.atomic_write(path, json.dumps({"n": n + 1}))
    except OSError:
        pass
    return n


def outbox_stage(workspace, now, config):
    state = outbox_audit.read_state(workspace, now.timestamp())
    fs = outbox_audit.audit(state, config, outbox_audit.load_prev(workspace))
    findings_mod.atomic_write(outbox_audit.prev_path(workspace),
                              json.dumps(outbox_audit.prev_of(state)))
    return fs


def orphan_stage(workspace, now, config):
    live = [b.get("proc_pid") for b in stall.read_beacons(workspace).values()
            if b.get("proc_pid") and procinfo.alive(b["proc_pid"])]
    orphans = procreap.find_orphans(live, workspace)
    reaped = None
    if config.get("reap_orphans") and orphans:
        reaped = procreap.reap_orphans(orphans)
    out = []
    for shard, items in orphans.items():
        o = max(items, key=lambda i: i.get("age_s") or 0)
        why = (f"{len(items)} process(es) left behind by shard {shard}: "
               f"\"{o['argv']}\" (pid {o['pid']}, {(o.get('age_s') or 0) / 60:.1f} min old)")
        if reaped is not None:
            why += f"; reaped: {len(reaped['termed'])} termed, {len(reaped['killed'])} killed"
        out.append(make("orphan-process", shard, "warn", why, detail={"orphans": items}))
    return out


def tick(workspace, now=None, send=None):
    """Run one pass. `send(post) -> bool` defaults to alerts.send. Returns the
    findings written."""
    workspace = Path(workspace)
    now = now or datetime.now(timezone.utc)
    found, posts = [], []
    config = {k: v for k, v in monitor_config.DEFAULTS.items()}

    def stage(name, fn):
        try:
            found.extend(fn() or [])
        except Exception as e:
            found.append(make("monitor-stage-failed", name, "warn",
                              f"monitor stage {name} failed: {e}",
                              now.isoformat(), {"trace": traceback.format_exc()[-800:]}))

    def cfg_stage():
        nonlocal config
        config, fs = monitor_config.load(workspace)
        return fs

    stage("config", cfg_stage)
    jobs_holder = {}

    def drift_stage():
        jfs = []
        jobs = job_registry.load_jobs(workspace, jfs)
        jobs_holder["jobs"] = jobs
        live = _load_json(_health(workspace) / "scheduler-jobs.json")
        started = heartbeats.parse_ts((live or {}).get("started_at"))
        return jfs + drift.drift_report(jobs, live, heartbeats.read_all(workspace, jobs), started, now)

    stage("drift", drift_stage)
    stage("stall", lambda: stall.stall_stage(workspace, now, config))
    slow = _next_count(workspace) % SLOW_EVERY == 0
    if slow:
        stage("outbox", lambda: outbox_stage(workspace, now, config))
        stage("orphans", lambda: orphan_stage(workspace, now, config))
    else:   # keep the last slow-stage findings until the next pass re-checks them
        found.extend(f for f in findings_mod.read(workspace) if f.kind in SLOW_KINDS)
    for name, fn in EXTRA_STAGES:
        stage(name, lambda fn=fn: fn(workspace, now, config))

    # keep `since` stable across ticks
    prev = {f.key: f.since for f in findings_mod.read(workspace)}
    for f in found:
        f.since = prev.get(f.key) or f.since or now.isoformat()

    state_path = _health(workspace) / "alert-state.json"
    old_state = _load_json(state_path) or {"keys": {}, "recent": []}
    try:
        posts, new_state = alerts.plan(found, old_state, config, now)
        do_send = send or (lambda p: alerts.send(p, config, workspace))
        results = []
        for p in posts:
            try:
                results.append(bool(do_send(p)))
            except Exception:
                results.append(False)
        findings_mod.atomic_write(state_path, json.dumps(alerts.commit(old_state, new_state, posts, results)))
    except Exception as e:
        found.append(make("monitor-stage-failed", "alerts", "warn",
                          f"monitor stage alerts failed: {e}", now.isoformat()))
    findings_mod.write(workspace, found, now.isoformat())
    return found


def run():
    tick(os.environ.get("WORKSPACE_ROOT") or os.environ.get("KARAKOS_HOME") or os.path.abspath(os.path.join(__file__, "..", "..")))
