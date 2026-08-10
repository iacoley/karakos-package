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
from datetime import datetime
from logging.handlers import RotatingFileHandler

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
HEALTH_FILE = WORKSPACE_ROOT / "data" / "health" / "scheduler.json"

# How often the loop wakes. Agent-scheduled oneshots are polled on every tick,
# so this is also the worst-case lateness of "remind me in 10 minutes". It used
# to be 60s, which was fine for jobs pinned to the hour and too coarse for a
# reminder a user is waiting on.
TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "15"))

# bin/ is not a package; import the oneshot primitive from this script's dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oneshot  # noqa: E402

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

def run_memory_maintenance():
    """Run memory consolidation"""
    log.info("Running memory maintenance")
    try:
        subprocess.run(
            ["python3", f"{WORKSPACE_ROOT}/bin/memory-maintenance.py"],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Memory maintenance failed: {e.stderr}")

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
    catches has a user waiting on the other end of it. Exit 1 means "wedged
    and alerted", which is a finding rather than an error, so it is not
    checked as a subprocess failure.
    """
    try:
        result = subprocess.run(
            ["python3", f"{WORKSPACE_ROOT}/bin/wedge-check.py"],
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


def check_updates():
    """Check for Karakos updates"""
    log.info("Checking for updates")
    try:
        subprocess.run(
            ["bash", f"{WORKSPACE_ROOT}/bin/check-updates.sh"],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Update check failed: {e.stderr}")

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

def main():
    """Main scheduler loop"""
    log.info("Scheduler starting")

    # Load agents config to get agent names
    agents_config_path = WORKSPACE_ROOT / "config" / "agents.json"
    if agents_config_path.exists():
        with open(agents_config_path) as f:
            config = json.load(f)
            agents = list(config.get("agents", {}).keys())
    else:
        agents = []
        log.warning("No agents config found")

    # Schedule heartbeats for each agent (staggered by 15 minutes)
    if agents:
        primary_agent = agents[0]
        schedule.every(30).minutes.do(lambda: run_heartbeat(primary_agent))
        log.info(f"Scheduled heartbeat for primary agent: {primary_agent}")

        # Schedule relay agent if exists
        if "relay" in agents:
            schedule.every(30).minutes.at(":15").do(lambda: run_heartbeat("relay"))
            log.info("Scheduled heartbeat for relay agent")

    # Schedule maintenance tasks
    schedule.every().day.at("03:00").do(run_memory_maintenance)
    schedule.every().day.at("04:00").do(run_health_monitor)
    # Every minute, not daily: the acceptance test for a wedged agent is an
    # alert within two minutes, and a check that runs at 04:00 cannot make
    # that promise at any other hour. It is a file read and a comparison.
    schedule.every(1).minutes.do(run_wedge_check)
    schedule.every().day.at("04:30").do(purge_old_data)
    schedule.every().monday.at("05:00").do(check_updates)  # Weekly update check

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

    # Main loop
    while True:
        try:
            schedule.run_pending()
            run_due_oneshots()
            write_health_timestamp()
            time.sleep(TICK_SECONDS)
        except KeyboardInterrupt:
            log.info("Scheduler shutting down")
            break
        except Exception as e:
            log.error(f"Scheduler error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
