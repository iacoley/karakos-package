#!/usr/bin/env python3
"""
Health Monitor — Checks component health and alerts on staleness
"""

import json
import logging
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import subprocess  # noqa: F401  (tests patch monitor.subprocess)
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

WORKSPACE_ROOT = Path(workspace_root())
HEALTH_DIR = WORKSPACE_ROOT / "data" / "health"

# Logging
log = logging.getLogger("health-monitor")
log.setLevel(logging.INFO)
handler = RotatingFileHandler(
    WORKSPACE_ROOT / "logs" / "health-alerts.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=3
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(handler)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(1, str(WORKSPACE_ROOT / "lib"))
import alerts  # noqa: E402
import heartbeats  # noqa: E402
import job_registry  # noqa: E402


def _health_files() -> dict:
    """filename -> threshold, from the job table: every component, plus jobs
    whose own script writes a health file. Add a job in lib/job_registry.py,
    not here."""
    out = {}
    for j in job_registry.load_jobs(WORKSPACE_ROOT):
        if j.kind == "component":
            out[f"{j.name}.json"] = j.max_age_s
        elif j.health_file:
            out[j.health_file] = j.max_age_s
    return out


def check_health_file(component: str, threshold: int) -> tuple[bool, str]:
    """Check if health file is fresh. Timestamp rules live in lib/heartbeats.py."""
    return heartbeats.check_component_file(HEALTH_DIR, component, threshold)

def poke_signals(message: str) -> bool:
    """Send alert to signals channel. Returns True only if it actually sent.

    Posts directly through bin/discord-notify.sh (lib/alerts.py), never by
    queueing for an agent: a queued poke lands in an agent's queue, and a wedged agent never reads
    it. Failures are logged to logs/health-alerts.log by alerts.send. The name
    is kept for bin/relay.py and the tests.
    """
    return alerts.send({"text": message, "channel": "signals"}, workspace=WORKSPACE_ROOT)


def verdict() -> dict:
    """The health verdict, as data. One implementation, two consumers.

    The scheduled run below posts to the signals channel with it; `--check`
    prints it as JSON for bin/relay.py's `/health` slash command. Computing
    it twice is how the alert and the command start disagreeing about
    whether the system is up.
    """
    issues = []
    components = {}
    for component, threshold in _health_files().items():
        ok, reason = check_health_file(component, threshold)
        components[component] = {"healthy": ok, "reason": reason}
        if not ok:
            issues.append(reason)
    return {"healthy": not issues, "issues": issues, "components": components}


def main():
    """Check all components and alert on issues"""
    if "--check" in sys.argv:
        # Read-only: no alert is sent, so an operator asking "is it healthy"
        # cannot spam the signals channel by asking twice.
        print(json.dumps(verdict()))
        return

    log.info("Running health monitor")

    result = verdict()
    issues = result["issues"]
    for reason in issues:
        log.warning(f"Health check failed: {reason}")

    if issues:
        alert = "⚠️ Health check failures:\n" + "\n".join(f"• {issue}" for issue in issues)
        if poke_signals(alert):
            log.info("Alert sent to signals channel")
        else:
            log.error("Health check failed AND the alert could not be sent")
    else:
        log.info("All components healthy")

if __name__ == "__main__":
    main()
