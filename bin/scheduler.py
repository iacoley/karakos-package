#!/usr/bin/env python3
"""
Python-based Scheduler — Replaces cron inside Docker

Runs scheduled tasks with full environment variable access.
Health heartbeat confirms liveness.
"""

import schedule
import subprocess
import sys
import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or os.environ.get("KARAKOS_HOME") or os.path.abspath(os.path.join(__file__, "..", "..")))
HEALTH_FILE = WORKSPACE_ROOT / "data" / "health" / "scheduler.json"

# How often the loop wakes. Agent-scheduled oneshots are polled on every tick,
# so this is also the worst-case lateness of "remind me in 10 minutes". It used
# to be 60s, which was fine for jobs pinned to the hour and too coarse for a
# reminder a user is waiting on.
TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "15"))

# bin/ is not a package; import the oneshot primitive from this script's dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import oneshot  # noqa: E402
import registry as agent_registry  # noqa: E402
import heartbeats  # noqa: E402
import job_registry  # noqa: E402

# Logging
log = logging.getLogger("scheduler")
log.setLevel(logging.INFO)
handler = RotatingFileHandler(
    WORKSPACE_ROOT / "logs" / "scheduler.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=7
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(handler)

# Also log to console
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(console)

def write_health_timestamp():
    """Write health heartbeat timestamp"""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_FILE, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "status": "healthy"
        }, f)

def run_heartbeat(agent: str):
    """Trigger heartbeat for agent"""
    log.info(f"Running heartbeat for {agent}")
    try:
        subprocess.run(
            [f"{WORKSPACE_ROOT}/bin/heartbeat.sh", agent],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Heartbeat failed for {agent}: {e.stderr}")

def run_health_monitor():
    """Run health monitor"""
    log.info("Running health monitor")
    try:
        subprocess.run(
            ["python3", f"{WORKSPACE_ROOT}/bin/health-monitor.py"],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Health monitor failed: {e.stderr}")

def run_wedge_check():
    """Check for agents that are alive but stuck.

    Runs every minute, unlike the daily health sweep, because the failure it
    catches has a user waiting on the other end of it. The monitor tick pages
    (finding stall:<shard>), so this run only diagnoses.
    Exit 1 means "wedged", a finding rather than an error, so it is not
    checked as a subprocess failure.
    """
    try:
        result = subprocess.run(
            ["python3", f"{WORKSPACE_ROOT}/bin/wedge-check.py", "--no-alert"],
            capture_output=True,
            text=True
        )
        if result.returncode == 1:
            log.warning(f"Wedged agent detected: {result.stdout.strip()}")
        elif result.returncode != 0:
            log.error(f"Wedge check failed ({result.returncode}): {result.stderr.strip()}")
    except OSError as e:
        log.error(f"Wedge check could not run: {e}")

def run_due_oneshots():
    """Fire any agent-scheduled oneshot whose absolute deadline has passed.

    Runs on every tick rather than on a schedule.every() job so that the
    granularity of "remind me in N minutes" is TICK_SECONDS, not the coarsest
    job interval.
    """
    try:
        oneshot.run_due(log=log, progress=write_health_timestamp)
    except Exception as e:
        log.error(f"Oneshot poll failed: {e}")


def replay_oneshots():
    """Re-arm the spool this container inherited from its previous life.

    The spool lives on the persistent data volume, so a restart does not lose
    pending work — but a deadline that passed while we were down needs an
    explicit decision (fire late vs. drop as stale), and that decision belongs
    at startup where it can be logged, not silently on the first tick.
    """
    try:
        result = oneshot.replay(log=log, progress=write_health_timestamp)
        log.info(
            "Oneshot spool restored: %d pending, %d fired late, %d dropped stale",
            len(result["rearmed"]), len(result["fired"]), len(result["dropped"]),
        )
    except Exception as e:
        log.error(f"Oneshot replay failed: {e}")

def run_cli_upgrade_watchdog():
    """Catch a Claude CLI upgrade that arrived without going through us.

    The CLI is not released by this project, and it changes underneath a
    running install on every `docker compose pull` onto a rebuilt image. A
    release that breaks the agent loop installs cleanly and reports a version,
    so nothing else here can see it — the symptom is silence.

    Exit 1 means "the upgrade was reverted and a notice was posted", which is
    a finding rather than an error, so it is not treated as a failed run.
    """
    try:
        result = subprocess.run(
            ["bash", f"{WORKSPACE_ROOT}/bin/cli-upgrade-watchdog.sh"],
            capture_output=True,
            text=True
        )
        if result.returncode == 1:
            log.warning(f"Claude CLI upgrade reverted: {result.stdout.strip()}")
        elif result.returncode == 2:
            log.error(f"Claude CLI revert FAILED: {result.stdout.strip()}")
        elif result.returncode not in (0, 3):
            log.error(f"CLI upgrade watchdog failed ({result.returncode}): {result.stderr.strip()}")
    except OSError as e:
        log.error(f"CLI upgrade watchdog could not run: {e}")

def run_flush_deferred_messages():
    """Re-fire inbound messages spooled while the agent server was down (#88).

    relay.py and poke.sh spool undeliverable /message payloads to
    data/deferred-messages/; this delivers them once the server is back. The
    cadence bounds how long a message sent during an outage waits after
    recovery.
    """
    try:
        subprocess.run(
            ["python3", f"{WORKSPACE_ROOT}/bin/flush-deferred-messages.py"],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Deferred-message flush failed: {e.stderr}")

def check_updates():
    """Check for Karakos updates.

    Exit 1 means the check could not complete — the releases API was
    unreachable, or an available update could not be announced. It is logged
    rather than swallowed (#152): this job runs weekly, so a checker that
    fails quietly stays broken for a month at a time, which is exactly how
    this one went unnoticed. Its own output goes to stdout, so that is logged
    alongside stderr — the reason usually lands there.
    """
    log.info("Checking for updates")
    try:
        result = subprocess.run(
            ["bash", f"{WORKSPACE_ROOT}/bin/check-updates.sh"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            log.error(
                f"Update check failed ({result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    except OSError as e:
        log.error(f"Update check could not run: {e}")

def purge_old_data():
    """Purge old logs and data"""
    log.info("Purging old data")
    try:
        subprocess.run(
            ["python3", f"{WORKSPACE_ROOT}/bin/purge-data.py"],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Data purge failed: {e.stderr}")

JOBS_FILE = WORKSPACE_ROOT / "data" / "health" / "scheduler-jobs.json"


def run_command(cmd):
    """Run a user job from config/jobs.yaml; a failure raises so it is recorded."""
    subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=str(WORKSPACE_ROOT))


def _runner(job, primary_agent, monitor_agent):
    """The callable for a job-table entry, or None if it cannot run here."""
    if isinstance(job.run, list):
        return lambda: run_command(job.run)
    if job.run == "scheduler:heartbeat-primary":
        return (lambda: run_heartbeat(primary_agent)) if primary_agent else None
    if job.run == "scheduler:heartbeat-monitor":
        return (lambda: run_heartbeat(monitor_agent)) if monitor_agent else None
    if isinstance(job.run, str):
        mod, _, fn = job.run.partition(":")
        if mod == "scheduler":  # this script is __main__; do not re-import it
            return globals().get(fn)
        return job_registry.resolve_callable(job.run)
    return None


def _wrap(job, fn):
    """Record every run in the job's heartbeat; exceptions are caught and logged."""
    def wrapped():
        try:
            fn()
            heartbeats.touch(WORKSPACE_ROOT, job.name, ok=True)
        except Exception as e:
            log.error(f"Job {job.name} failed: {e}")
            try:
                heartbeats.touch(WORKSPACE_ROOT, job.name, ok=False, detail=str(e)[:300])
            except OSError:
                pass
    return wrapped


_last_jobs_doc = None


def write_jobs_file(started_at, sched_jobs):
    """data/health/scheduler-jobs.json: what is actually scheduled, for drift."""
    global _last_jobs_doc
    nxt = {}
    for j in sched_jobs:
        name = next(iter(j.tags), None)
        if name and j.next_run and (name not in nxt or j.next_run < nxt[name]):
            nxt[name] = j.next_run
    doc = {"started_at": started_at,
           "jobs": [{"name": n, "next_run": t.isoformat()} for n, t in sorted(nxt.items())]}
    if doc == _last_jobs_doc:
        return
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_FILE.with_name(JOBS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, JOBS_FILE)
        _last_jobs_doc = doc
    except OSError as e:
        log.error(f"Could not write scheduler-jobs.json: {e}")


def main():
    """Main scheduler loop"""
    log.info("Scheduler starting")
    started_at = datetime.now(timezone.utc).isoformat()

    # Load the registry: the primary and the monitor each get a heartbeat
    try:
        reg = agent_registry.load_registry(WORKSPACE_ROOT)
        primary_agent = reg.primary().id
        monitor_agent = reg.monitor().id
    except agent_registry.RegistryError as e:
        primary_agent = monitor_agent = None
        log.warning(f"No usable agent registry: {e}")

    # Every scheduled job comes from lib/job_registry.py (and config/jobs.yaml),
    # the same table the monitor checks. Heartbeats are at minute marks 0/30
    # (primary) and 15/45 (monitor); the old `.at(":15")` set seconds, not minutes.
    wrapped = {}
    sched_jobs = []
    for job in job_registry.load_jobs(WORKSPACE_ROOT):
        if job.kind != "job":
            continue
        try:
            fn = _runner(job, primary_agent, monitor_agent)
            if fn is None:
                log.warning(f"Job {job.name} has no runnable target here; not scheduled")
                continue
            wrapped[job.name] = _wrap(job, fn)
            sched_jobs += job_registry.apply_schedule(schedule.default_scheduler, job, wrapped[job.name])
            log.info(f"Scheduled job {job.name}")
        except Exception as e:
            log.error(f"Could not schedule job {job.name}: {e}")
    write_jobs_file(started_at, sched_jobs)

    # Declare liveness BEFORE the replay, not after. Replay fires every
    # deadline missed while we were down, synchronously, and the longer the
    # outage the more there are to fire — so the slowest replay is the one
    # that happens right after the longest downtime. With no health file
    # written yet, health-monitor reads "missing" and calls us dead at the
    # exact moment we are recovering. The batch refreshes it per entry too.
    write_health_timestamp()

    # Before the first tick: whatever the previous container left in the spool.
    replay_oneshots()

    log.info("Scheduler configured, entering main loop")

    # Run the CLI watchdog once immediately. Startup is when drift is most
    # likely — the container has just come up on a freshly pulled image, which
    # is how a new Claude CLI actually arrives. Waiting for the first hourly
    # tick would leave a broken CLI unchallenged for an hour of the exact
    # window it is most likely to be broken in.
    if "cli-watchdog" in wrapped:
        run_cli_upgrade_watchdog()

    # Main loop
    while True:
        try:
            schedule.run_pending()
            run_due_oneshots()
            write_health_timestamp()
            write_jobs_file(started_at, sched_jobs)
            time.sleep(TICK_SECONDS)
        except KeyboardInterrupt:
            log.info("Scheduler shutting down")
            break
        except Exception as e:
            log.error(f"Scheduler error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
