#!/usr/bin/env python3
"""
wedge-check.py — catch an agent that is alive and stuck.

health-monitor.py checks whether components are *running*. This checks
whether a running agent is *moving*, which is a different failure and the one
that actually strands a user: the claude subprocess is up, the agent server's
event loop is fine, /health answers 200, the port is open — and the messages
go nowhere. Nothing in the existing checks can see it, because from outside
every liveness signal is green.

Three properties, each of which the design turns on:

**It runs outside the process it is checking.** A check that runs inside a
wedged process is not a check. This is a separate script on its own schedule,
reading files.

**It alerts direct to Discord, never through poke.sh.** poke.sh queues a
message *for an agent*. If that agent is the wedged one, the alert lands in
the queue it cannot read and is never seen — the check would fail silently in
exactly the case it exists for. bin/discord-notify.sh posts with the bot token
and never touches the queue.

**It never restarts anything.** Detection and escalation only, deliberately.
An automatic restart of a wedged agent discards whatever it was doing, and
the operator, not this script, gets to decide that.

Exit codes:
  0 — no agent is wedged (or nothing is running yet)
  1 — at least one agent is wedged; alert sent

Flags:
  --threshold SEC   silence tolerated while PROCESSING (default 120)
  --no-alert        diagnose only; do not post to Discord
  --json            machine-readable output
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
BEACON_DIR = WORKSPACE_ROOT / "data" / "health" / "agents"
STATE_PATH = WORKSPACE_ROOT / "data" / "health" / "wedge-check-state.json"
ALERT_CHANNEL = os.environ.get("WEDGE_ALERT_CHANNEL", "signals")

# How long an agent may be silent while still claiming to be PROCESSING.
#
# The acceptance test wants an alert within two minutes of a SIGSTOP, which
# sets the ceiling. The floor is a legitimately quiet stretch mid-turn — a
# long Bash call emits nothing until it returns. 120s sits between them, and
# the consequence of guessing low is a message to a human, not a restart:
# this script never acts on its own finding, so a false positive costs
# attention rather than work.
DEFAULT_THRESHOLD_SEC = 120

# Only these states mean "a turn was claimed". An IDLE agent is silent for
# hours and that is correct — treating idle silence as a wedge would page
# every night, and a check that cries wolf is worse than no check.
ACTIVE_STATES = frozenset({"PROCESSING", "ERROR_RECOVERY"})


def read_beacon(path: Path):
    """Parse one beacon. Returns None if it cannot be read or trusted.

    An unreadable beacon is explicitly NOT a wedge. The beacon is written
    atomically via rename, so a malformed one means something else is wrong,
    and inventing a wedge from it would page for the wrong reason.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        last = datetime.fromisoformat(data["last_activity"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "agent": data.get("agent") or path.stem,
        "state": data.get("state"),
        "message_id": data.get("message_id"),
        "pid": data.get("pid"),
        "last_activity": last,
    }


def find_wedged(threshold_sec, now=None):
    """Return a list of wedged-agent records, newest silence first."""
    now = datetime.now() if now is None else now
    if not BEACON_DIR.is_dir():
        return []

    wedged = []
    for path in sorted(BEACON_DIR.glob("*.json")):
        beacon = read_beacon(path)
        if beacon is None:
            continue
        if beacon["state"] not in ACTIVE_STATES:
            continue
        silent_for = (now - beacon["last_activity"]).total_seconds()
        if silent_for <= threshold_sec:
            continue
        beacon["silent_for"] = silent_for
        wedged.append(beacon)

    wedged.sort(key=lambda b: b["silent_for"], reverse=True)
    return wedged


def _load_state():
    try:
        data = json.loads(STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError:
        pass


def alert_key(beacon):
    """Identity of one wedge episode.

    Keyed on the frozen timestamp, not on the agent: while an agent stays
    wedged its `last_activity` does not move, so a check running every minute
    recognises the same episode and stays quiet. If it recovers and wedges
    again the timestamp is new, and that is a new page — which it should be.
    """
    return f"{beacon['agent']}@{beacon['last_activity'].isoformat()}"


def send_alert(message):
    """Post direct to Discord. Never poke.sh — see the module docstring."""
    notify = WORKSPACE_ROOT / "bin" / "discord-notify.sh"
    if not notify.exists():
        print(f"wedge-check: no discord-notify.sh, alert not sent: {message}", file=sys.stderr)
        return False
    try:
        subprocess.run([str(notify), ALERT_CHANNEL, message],
                       check=True, capture_output=True, timeout=30)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        print(f"wedge-check: alert failed: {e}", file=sys.stderr)
        return False


def format_alert(wedged):
    lines = ["🚨 Agent wedged — alive but not processing. **No restart has been attempted.**"]
    for beacon in wedged:
        minutes = beacon["silent_for"] / 60
        lines.append(
            f"• `{beacon['agent']}` — {beacon['state']}, silent {minutes:.1f} min"
            + (f" (message {beacon['message_id']})" if beacon.get("message_id") else "")
            + (f", pid {beacon['pid']}" if beacon.get("pid") else "")
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_SEC)
    parser.add_argument("--no-alert", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    wedged = find_wedged(args.threshold)

    if args.json:
        print(json.dumps({
            "wedged": [
                {**b, "last_activity": b["last_activity"].isoformat()} for b in wedged
            ]
        }, indent=2))
    elif wedged:
        print(format_alert(wedged))
    else:
        print("No wedged agents.")

    if not wedged:
        return 0

    if not args.no_alert:
        state = _load_state()
        already = set(state.get("alerted", []))
        fresh = [b for b in wedged if alert_key(b) not in already]
        if fresh:
            if send_alert(format_alert(fresh)):
                # Only the episodes just reported are remembered, so a wedge
                # that recovers and recurs pages again.
                state["alerted"] = sorted(already | {alert_key(b) for b in fresh})[-100:]
                _save_state(state)

    return 1


if __name__ == "__main__":
    sys.exit(main())
