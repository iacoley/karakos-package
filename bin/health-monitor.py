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
from datetime import datetime, timedelta, timezone
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

# Component health thresholds (in seconds).
#
# Every key here must be a file some component actually writes. `memory.json`
# sat in this table until 2026-08-09 and nothing had ever written it —
# memory-maintenance.py writes `memory-maintenance.json` (docs/ARCHITECTURE.md
# has always said so). A threshold naming a file with no writer is not a check;
# it is a permanent "health file missing" that trains you to ignore the alert.
THRESHOLDS = {
    "mcp-tools.json": 600,            # 10 minutes
    "relay.json": 300,                 # 5 minutes
    "memory-maintenance.json": 172800, # 48 hours — only runs daily
    "scheduler.json": 300,             # 5 minutes
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
        # Writers disagree about tzinfo and always have: scheduler.py and
        # relay.py write naive local `datetime.now()`, while tools-server.py
        # writes aware `datetime.now(timezone.utc)`. Subtracting one from the
        # other raises TypeError, which the except below turned into
        # "mcp-tools.json error: can't subtract offset-naive and offset-aware
        # datetimes" — so a perfectly healthy MCP tools server reported
        # unhealthy on every single run, and had since the aware writer landed.
        # Normalise instead of trusting the writers to agree. A naive stamp is
        # local time, which is what the naive writers mean by it.
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()

        if age > threshold:
            return False, f"{component} stale ({age/60:.1f} min, threshold {threshold/60:.1f} min)"

        return True, ""

    except Exception as e:
        return False, f"{component} error: {e}"

def poke_signals(message: str) -> bool:
    """Send alert to signals channel. Returns True only if it actually sent.

    The caller logs the outcome, so this has to report one. It used to swallow
    the failure and return None, and main() then logged "Alert sent to signals
    channel" unconditionally — the log claimed delivery on runs where nothing
    was delivered, which is the one thing you go to that log to find out.
    """
    try:
        subprocess.run(
            [
                f"{WORKSPACE_ROOT}/bin/poke.sh",
                "--reply-channel", "signals",
                "--source", "health-monitor",
                message
            ],
            check=True,
            capture_output=True,
            # poke.sh talks to Discord. Without a timeout a hung request wedges
            # the monitor forever, and this runs on a schedule.
            timeout=30,
        )
        return True
    except subprocess.CalledProcessError as e:
        # capture_output already paid for stderr; not logging it throws away
        # the only description of why the alert failed.
        stderr = (e.stderr or b"").decode(errors="replace").strip()
        log.error(f"Failed to poke signals (exit {e.returncode}): {stderr or e}")
    except subprocess.TimeoutExpired:
        log.error("Failed to poke signals: poke.sh timed out after 30s")
    except OSError as e:
        # Missing/non-executable poke.sh raises here, not CalledProcessError.
        # Uncaught, it took down the whole monitor instead of one alert.
        log.error(f"Failed to poke signals: cannot run poke.sh: {e}")
    return False

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
        if poke_signals(alert):
            log.info("Alert sent to signals channel")
        else:
            log.error("Health check failed AND the alert could not be sent")
    else:
        log.info("All components healthy")

if __name__ == "__main__":
    main()
