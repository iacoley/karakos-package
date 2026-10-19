"""One table of what must run: the scheduler registers from it and the monitor
checks it, so the two cannot disagree.

`kind="job"`: the scheduler runs it and `lib/heartbeats.py` records each run.
`kind="component"`: another process writes `data/health/<name>.json`.

`register`/`unregister` are the seam later steps use (a job module appends its
`Job` at import time; list the module in `OPTIONAL_MODULES`).
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Union

import yaml

log = logging.getLogger("job_registry")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_ATTR = {"mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday",
            "fri": "friday", "sat": "saturday", "sun": "sunday"}


@dataclass(frozen=True)
class Every:
    seconds: int
    offset_s: int = 0


@dataclass(frozen=True)
class Daily:
    at: str


@dataclass(frozen=True)
class Weekly:
    day: str
    at: str


Schedule = Union[Every, Daily, Weekly, None]


@dataclass(frozen=True)
class Job:
    name: str
    kind: str = "job"                    # job | component
    schedule: Schedule = None
    run: Union[str, list, None] = None   # "module:function" or a command list
    max_age_s: int = 3600
    critical: bool = False
    owner: str = "monitor"               # a role
    health_file: Optional[str] = None    # extra file a job's own script writes


H = 3600
BUILTIN = [
    Job("heartbeat-primary", "job", Every(1800, 0), "scheduler:heartbeat-primary", 5400),
    Job("heartbeat-monitor", "job", Every(1800, 900), "scheduler:heartbeat-monitor", 5400),
    Job("memory-consolidate", "job", Daily("03:00"),
        "monitor_jobs.memory_consolidate:run_scheduled", 48 * H,
        health_file="memory-consolidate.json"),
    Job("health-sweep", "job", Daily("04:00"), "scheduler:run_health_monitor", 48 * H),
    Job("wedge-check", "job", Every(60), "scheduler:run_wedge_check", 300),
    Job("cli-watchdog", "job", Every(3600), "scheduler:run_cli_upgrade_watchdog", 2 * H),
    Job("flush-deferred", "job", Every(300), "scheduler:run_flush_deferred_messages", 900),
    Job("purge", "job", Daily("04:30"), "scheduler:purge_old_data", 48 * H),
    Job("update-check", "job", Weekly("mon", "05:00"), "scheduler:check_updates", 14 * 24 * H),
    Job("monitor-tick", "job", Every(60), "monitor_tick:run", 300),
    Job("friction-sensor", "job", Every(3600, 600),
        "monitor_jobs.friction_sensor:run_scheduled", 3 * H),
    Job("friction-review", "job", Daily("03:30"),
        "monitor_jobs.friction_review:run_scheduled", 48 * H),
    Job("scheduler", "component", None, None, 300, critical=True),
    Job("mcp-tools", "component", None, None, 600, critical=True),
    Job("relay", "component", None, None, 300, critical=True),
]

_TABLE: dict = {}
OPTIONAL_MODULES: list = []   # module names that call register() at import


def register(job: Job) -> None:
    _TABLE[job.name] = job


def unregister(name: str) -> None:
    _TABLE.pop(name, None)


def _reset() -> None:
    _TABLE.clear()
    for j in BUILTIN:
        _TABLE[j.name] = j


def _import_optional() -> None:
    for mod in list(OPTIONAL_MODULES):
        try:
            importlib.import_module(mod)
        except Exception as e:  # missing or broken module is tolerated
            log.warning("optional job module %s skipped: %s", mod, e)


_reset()


def hourly_marks(interval_min: int, offset_min: int) -> list:
    """Minute marks within an hour for an every-N-minutes job with an offset."""
    if interval_min <= 0 or 60 % interval_min != 0:
        raise ValueError(f"interval {interval_min} min does not divide 60")
    return sorted((offset_min + k * interval_min) % 60 for k in range(60 // interval_min))


def uses_marks(every: Every) -> bool:
    """Marks are used when the interval divides an hour and either has an
    offset or is at least 10 minutes. Other intervals use every(n).seconds,
    which has no offset support (an offset there raises)."""
    if every.seconds % 60 or 3600 % every.seconds:
        return False
    return bool(every.offset_s) or every.seconds >= 600


# ---- config/jobs.yaml ------------------------------------------------------

def parse_duration(v) -> int:
    if isinstance(v, bool):
        raise ValueError("bad duration")
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def _parse_schedule(spec: dict, current: Schedule = None) -> Schedule:
    if "every" in spec:
        off = current.offset_s if isinstance(current, Every) else 0
        return Every(parse_duration(spec["every"]), off)
    if "daily" in spec:
        return Daily(str(spec["daily"]))
    if "weekly" in spec:
        day, at = str(spec["weekly"]).split()
        if day.lower()[:3] not in DAYS:
            raise ValueError(f"bad weekday {day}")
        return Weekly(day.lower()[:3], at)
    return current


def _default_max_age(sched: Schedule) -> int:
    if isinstance(sched, Every):
        return sched.seconds * 3 + 60
    if isinstance(sched, Weekly):
        return 8 * 86400
    return 48 * H


_KEYS = {"every", "daily", "weekly", "command", "max_age", "critical"}


def load_jobs(workspace, findings: Optional[list] = None) -> list:
    """The job table with config/jobs.yaml applied. An invalid file yields the
    builtin table and appends a `jobs-config-invalid` Finding to `findings`."""
    _import_optional()
    base = dict(_TABLE)
    path = Path(workspace) / "config" / "jobs.yaml"
    if not path.is_file():
        return list(base.values())
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
        if not isinstance(cfg, dict):
            raise ValueError("top level must be a mapping")
        for k in cfg:
            if k not in ("disable", "jobs", "override"):
                log.warning("jobs.yaml: unknown key %r ignored", k)
        out = dict(base)
        for name, spec in (cfg.get("jobs") or {}).items():
            if not isinstance(spec, dict):
                raise ValueError(f"job {name}: must be a mapping")
            for k in spec:
                if k not in _KEYS:
                    log.warning("jobs.yaml: job %s: unknown key %r ignored", name, k)
            cmd = spec.get("command")
            if not isinstance(cmd, list) or not cmd:
                raise ValueError(f"job {name}: command must be a non-empty list")
            sched = _parse_schedule(spec)
            if sched is None:
                raise ValueError(f"job {name}: needs every, daily or weekly")
            ma = parse_duration(spec["max_age"]) if "max_age" in spec else _default_max_age(sched)
            out[name] = Job(name, "job", sched, [str(c) for c in cmd], ma,
                            bool(spec.get("critical", False)))
        for name, spec in (cfg.get("override") or {}).items():
            if name not in out:
                log.warning("jobs.yaml: override for unknown job %s ignored", name)
                continue
            if not isinstance(spec, dict):
                raise ValueError(f"override {name}: must be a mapping")
            j = out[name]
            for k in spec:
                if k not in ("max_age", "every", "daily", "weekly", "critical"):
                    log.warning("jobs.yaml: override %s: unknown key %r ignored", name, k)
            if "max_age" in spec:
                j = replace(j, max_age_s=parse_duration(spec["max_age"]))
            if "critical" in spec:
                j = replace(j, critical=bool(spec["critical"]))
            sched = _parse_schedule(spec, j.schedule)
            if sched is not j.schedule:
                j = replace(j, schedule=sched)
            out[name] = j
        for name in cfg.get("disable") or []:
            out.pop(name, None)
        return list(out.values())
    except Exception as e:
        if findings is not None:
            from findings import Finding
            findings.append(Finding("jobs-config-invalid", "jobs-config-invalid", "warn",
                                    "config/jobs.yaml", f"config/jobs.yaml is invalid: {e}",
                                    detail={"error": str(e)}))
        return list(base.values())


# ---- scheduling ------------------------------------------------------------

def apply_schedule(sched, job: Job, fn) -> list:
    """Register `fn` on a `schedule.Scheduler` per `job.schedule`. Returns the
    schedule jobs created (tagged with the job name)."""
    s = job.schedule
    made = []
    if isinstance(s, Every):
        if uses_marks(s):
            for m in hourly_marks(s.seconds // 60, s.offset_s // 60):
                made.append(sched.every().hour.at(f":{m:02d}").do(fn))
        else:
            if s.offset_s:
                raise ValueError(f"{job.name}: offset not supported for a {s.seconds}s interval")
            made.append(sched.every(s.seconds).seconds.do(fn))
    elif isinstance(s, Daily):
        made.append(sched.every().day.at(s.at).do(fn))
    elif isinstance(s, Weekly):
        made.append(getattr(sched.every(), DAY_ATTR[s.day]).at(s.at).do(fn))
    else:
        raise ValueError(f"{job.name}: no schedule")
    for m in made:
        m.tag(job.name)
    return made


def resolve_callable(dotted: str):
    mod, _, fn = dotted.partition(":")
    return getattr(importlib.import_module(mod), fn)
