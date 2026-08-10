#!/usr/bin/env python3
"""
Health Monitor — Checks component health and alerts on staleness
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
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

# Component health thresholds (in seconds)
THRESHOLDS = {
    "mcp-tools.json": 600,       # 10 minutes
    "relay.json": 300,            # 5 minutes
    "memory.json": 172800,        # 48 hours
    "scheduler.json": 300,        # 5 minutes
}

def check_health_file(component: str, threshold: int) -> tuple[bool, str]:
    """Check if health file is fresh"""
    health_file = HEALTH_DIR / component

    if not health_file.exists():
        return False, f"{component} health file missing"

    try:
        with open(health_file) as f:
            data = json.load(f)
            timestamp_str = data.get("timestamp", "")

        if not timestamp_str:
            return False, f"{component} has no timestamp"

        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        age = (datetime.now() - timestamp).total_seconds()

        if age > threshold:
            return False, f"{component} stale ({age/60:.1f} min, threshold {threshold/60:.1f} min)"

        return True, ""

    except Exception as e:
        return False, f"{component} error: {e}"

def poke_signals(message: str):
    """Send alert to signals channel"""
    try:
        subprocess.run(
            [
                f"{WORKSPACE_ROOT}/bin/poke.sh",
                "--reply-channel", "signals",
                "--source", "health-monitor",
                message
            ],
            check=True,
            capture_output=True
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Failed to poke signals: {e}")

def verdict() -> dict:
    """The health verdict, as data. One implementation, two consumers.

    The scheduled run below pokes the signals channel with it; `--check`
    prints it as JSON for bin/relay.py's `/health` slash command. Computing
    it twice is how the alert and the command start disagreeing about
    whether the system is up.
    """
    issues = []
    components = {}
    for component, threshold in THRESHOLDS.items():
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
        poke_signals(alert)
        log.info("Alert sent to signals channel")
    else:
        log.info("All components healthy")

if __name__ == "__main__":
    main()
