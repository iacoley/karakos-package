#!/usr/bin/env python3
"""
Oneshot scheduler — the primitive that makes "I'll check back in 10 minutes"
a mechanism instead of a sentence.

An agent schedules an arbitrary future command; the command runs at (or after)
its deadline, and it survives a container restart. Nothing else in this package
lets an agent schedule arbitrary future work: bin/scheduler.py runs a fixed set
of hardcoded jobs, and a promise made in prose is unbacked.

Why not systemd transient timers
--------------------------------
The household original (schedule-oneshot.sh + oneshot-exec.sh +
oneshot-replay.sh + cancel-oneshot.sh) arms `systemd-run --user` timers and
replays the spool at boot because transient timers live in /run and die.
Karakos ships as a Docker image where PID 1 is supervisord and there is no
systemd, user bus, or /run/user/$UID at all. So the timer half is reimplemented
here as a poll over the same on-disk spool, driven by the already-running
bin/scheduler.py process. Everything that made the original survive a reboot is
kept verbatim in spirit:

  - the spool entry stores the COMPUTED ABSOLUTE fire time, never the relative
    spec the caller typed. Re-arming "+10min" after a restart would silently
    slip the deadline by however long the container was down.
  - the spool lives under data/, which is a persistent Docker volume, so it
    outlives the container it was written in.
  - startup re-reads the spool and decides, per entry, whether it is still
    pending, was missed while down, or is too stale to be worth firing.

Missed deadlines (the deliberate choice)
----------------------------------------
An entry whose deadline passed while the container was down FIRES IMMEDIATELY,
as long as it is less than ONESHOT_STALE_AFTER_SECONDS late (default 24h).
Beyond that it is DROPPED with a log line. Rationale: a reminder twenty minutes
late is still the thing the user asked for; a reminder three days late is noise,
and a week-long outage that ends with fifty stale reminders arriving at once is
worse than silence. The cutoff is one knob, logged in both directions.

An entry is unlinked from the spool BEFORE its command runs. Fired is fired —
if the container dies mid-command, the next start must not run a side-effectful
command a second time.

CLI
---
  oneshot.py schedule --label LABEL --when WHEN (--command CMD | --message TEXT)
  oneshot.py list
  oneshot.py cancel LABEL [LABEL...]
  oneshot.py run-due          # fire everything due now (one pass)
  oneshot.py replay           # startup pass: report pending, fire missed

Add --json to any subcommand for machine-readable output.

Env:
  WORKSPACE_ROOT                 workspace root (default /workspace)
  ONESHOT_SPOOL_DIR              override spool directory (mainly for tests)
  ONESHOT_STALE_AFTER_SECONDS    drop entries later than this (default 86400)
  ONESHOT_EXEC_TIMEOUT           per-command timeout in seconds (default 60)
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))

SPOOL_SUFFIX = ".oneshot.json"
MAX_COMMAND_LEN = 8192


def spool_dir() -> Path:
    """Resolved at call time, not import time, so tests can point it elsewhere."""
    override = os.environ.get("ONESHOT_SPOOL_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("WORKSPACE_ROOT", "/workspace")) / "data" / "oneshot-spool"


def stale_after_seconds() -> int:
    return int(os.environ.get("ONESHOT_STALE_AFTER_SECONDS", str(24 * 60 * 60)))


def exec_timeout() -> int:
    return int(os.environ.get("ONESHOT_EXEC_TIMEOUT", "60"))


class OneshotError(Exception):
    """A caller-facing scheduling error (bad spec, duplicate label, ...)."""


# =============================================================================
# Time parsing
# =============================================================================

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_RELATIVE_PART = re.compile(r"(\d+)\s*([a-z]+)")


def parse_relative(spec: str) -> int | None:
    """Parse '10m', '+10min', '1h30m', '90s' into seconds. None if not relative."""
    text = spec.strip().lower()
    if text.startswith("+"):
        text = text[1:].strip()
    if not text or not text[0].isdigit():
        return None

    total = 0
    consumed = 0
    for match in _RELATIVE_PART.finditer(text):
        if match.start() != consumed:
            return None
        unit = match.group(2)
        if unit not in _UNIT_SECONDS:
            return None
        total += int(match.group(1)) * _UNIT_SECONDS[unit]
        consumed = match.end()
        while consumed < len(text) and text[consumed] in " ,":
            consumed += 1
    if consumed != len(text) or total == 0:
        return None
    return total


def resolve_fire_at(when: str, now: float | None = None) -> int:
    """Turn a WHEN spec into an ABSOLUTE epoch second.

    This is the load-bearing line of the whole feature. Everything downstream
    stores and compares absolute times; the relative spec the caller typed is
    kept only as a human-readable note on the entry.
    """
    now = time.time() if now is None else now

    delta = parse_relative(when)
    if delta is not None:
        return int(now + delta)

    text = when.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt in ("%H:%M:%S", "%H:%M"):
                # A bare clock time means the next occurrence of it, not
                # 1900-01-01 — and not a time that already passed today.
                today = datetime.fromtimestamp(now)
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
                if parsed.timestamp() <= now:
                    parsed = datetime.fromtimestamp(parsed.timestamp() + 86400)
            break
    if parsed is None:
        raise OneshotError(
            f"Could not parse time spec {when!r}. Use a relative span "
            "('10m', '+2h', '1h30m') or an absolute time ('2026-08-09 09:00')."
        )

    if parsed.tzinfo is not None:
        return int(parsed.timestamp())
    return int(parsed.timestamp())


# =============================================================================
# Spool
# =============================================================================

def sanitize_label(label: str) -> str:
    """Same shape as the household original: spaces and slashes to dashes,
    then anything that is not alnum or dash is dropped. Cancel must sanitize
    identically or it silently cancels nothing."""
    label = label.strip()
    label = label.replace(" ", "-").replace("/", "-")
    cleaned = re.sub(r"[^A-Za-z0-9-]", "", label)
    cleaned = cleaned.strip("-")
    if not cleaned:
        raise OneshotError(f"Label {label!r} has no usable characters")
    return cleaned[:80]


def entry_id(label: str) -> str:
    return f"oneshot-{sanitize_label(label)}"


def entry_path(label: str, directory: Path | None = None) -> Path:
    directory = spool_dir() if directory is None else directory
    return directory / f"{entry_id(label)}{SPOOL_SUFFIX}"


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def load_entries(directory: Path | None = None) -> list[dict]:
    """Read every well-formed spool entry. Malformed files are quarantined
    (renamed to .malformed) rather than left to be re-read and re-logged on
    every tick, and rather than deleted — a human may want to see them."""
    directory = spool_dir() if directory is None else directory
    if not directory.is_dir():
        return []

    entries = []
    for path in sorted(directory.glob(f"*{SPOOL_SUFFIX}")):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("entry is not an object")
            if not isinstance(data.get("fire_at"), int):
                raise ValueError("missing integer fire_at")
            if not data.get("command"):
                raise ValueError("missing command")
        except Exception as exc:
            quarantine = path.with_suffix(path.suffix + ".malformed")
            try:
                os.replace(path, quarantine)
            except OSError:
                pass
            print(f"MALFORMED spool entry {path.name} ({exc}); quarantined as "
                  f"{quarantine.name}", file=sys.stderr)
            continue
        data["_path"] = str(path)
        entries.append(data)

    entries.sort(key=lambda e: e["fire_at"])
    return entries


def poke_command(message: str, agent: str | None = None,
                 reply_channel: str | None = None) -> str:
    """Build the poke.sh invocation that delivers an unprompted message.

    This is what makes "remind me in 10 minutes" land as a message rather than
    as a log line no one reads.
    """
    workspace = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
    parts = ["bash", str(workspace / "bin" / "poke.sh"), "--source", "scheduler"]
    if agent:
        parts += ["--agent", agent]
    if reply_channel:
        parts += ["--reply-channel", reply_channel]
    parts.append(message)
    return " ".join(shlex.quote(p) for p in parts)


def schedule(label: str, when: str, command: str | None = None,
             message: str | None = None, agent: str | None = None,
             reply_channel: str | None = None, replace: bool = False,
             now: float | None = None, directory: Path | None = None) -> dict:
    """Spool a future command with its absolute fire time. Returns the entry."""
    if not command and not message:
        raise OneshotError("Provide either a command or a message to deliver")
    if command and message:
        raise OneshotError("Provide a command or a message, not both")

    if message:
        command = poke_command(message, agent=agent, reply_channel=reply_channel)
    if len(command) > MAX_COMMAND_LEN:
        raise OneshotError(f"Command too long ({len(command)} > {MAX_COMMAND_LEN})")

    now = time.time() if now is None else now
    fire_at = resolve_fire_at(when, now=now)
    if fire_at <= now:
        raise OneshotError(
            f"{when!r} resolves to {datetime.fromtimestamp(fire_at).isoformat()}, "
            "which is in the past"
        )

    directory = spool_dir() if directory is None else directory
    path = entry_path(label, directory)
    if path.exists() and not replace:
        raise OneshotError(
            f"A oneshot labeled {sanitize_label(label)!r} is already pending "
            f"({path}). Cancel it first or pass replace."
        )

    entry = {
        "id": entry_id(label),
        "label": label,
        "when": when,
        "fire_at": fire_at,
        "fire_at_iso": datetime.fromtimestamp(fire_at, timezone.utc).isoformat(),
        "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "command": command,
        "agent": agent,
        "message": message,
    }
    _write_atomic(path, entry)
    entry["_path"] = str(path)
    return entry


def cancel(labels: list[str], directory: Path | None = None) -> dict:
    """Remove spool entries. With no timers to stop, cancel is exactly one
    action — but it must sanitize the label the same way schedule() did, and
    it must tolerate the id/filename forms a caller has in hand after `list`."""
    directory = spool_dir() if directory is None else directory
    cancelled, missing = [], []
    for raw in labels:
        label = raw
        for suffix in (SPOOL_SUFFIX, ".json", ".oneshot"):
            if label.endswith(suffix):
                label = label[: -len(suffix)]
        if label.startswith("oneshot-"):
            label = label[len("oneshot-"):]
        try:
            path = entry_path(label, directory)
        except OneshotError:
            missing.append(raw)
            continue
        if path.exists():
            path.unlink()
            cancelled.append(entry_id(label))
        else:
            missing.append(raw)
    return {"cancelled": cancelled, "not_found": missing}


# =============================================================================
# Firing
# =============================================================================

def _run(command: str) -> int:
    workspace = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
    cwd = str(workspace) if workspace.is_dir() else None
    try:
        result = subprocess.run(
            ["/bin/bash", "-c", command],
            capture_output=True, text=True, timeout=exec_timeout(), cwd=cwd,
        )
        if result.returncode != 0:
            print(f"oneshot command exited {result.returncode}: "
                  f"{result.stderr.strip()[:500]}", file=sys.stderr)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"oneshot command timed out after {exec_timeout()}s", file=sys.stderr)
        return 124
    except OSError as exc:
        print(f"oneshot command could not run: {exc}", file=sys.stderr)
        return 127


def run_due(now: float | None = None, directory: Path | None = None,
            runner=None, log=None, progress=None) -> list[dict]:
    """Fire every entry whose absolute deadline has passed. One pass.

    Called on every scheduler tick AND from replay() at startup — the same
    code path, so the reboot case cannot drift away from the steady-state case.

    `progress` is called after each entry is processed. Firing is synchronous
    and each command may burn the full ONESHOT_EXEC_TIMEOUT (60s), so a batch
    of due entries blocks the caller for minutes. The scheduler's liveness
    heartbeat is on that same thread, and health-monitor calls it dead at 300s
    — five slow pokes would have the scheduler reported wedged while it is
    doing exactly its job. The callback lets the caller stay alive mid-batch.
    """
    now = time.time() if now is None else now
    runner = _run if runner is None else runner
    directory = spool_dir() if directory is None else directory
    stale_cutoff = stale_after_seconds()

    def tick():
        if progress:
            try:
                progress()
            except Exception:
                # A liveness callback must never be able to abort the batch it
                # is only observing.
                pass

    results = []
    for entry in load_entries(directory):
        if entry["fire_at"] > now:
            continue

        path = Path(entry["_path"])
        late = int(now - entry["fire_at"])

        # Unlink BEFORE running: fired is fired. A crash mid-command must not
        # re-run a side-effectful command on the next start.
        try:
            path.unlink()
        except FileNotFoundError:
            continue

        if late > stale_cutoff:
            record = {"id": entry["id"], "label": entry.get("label"),
                      "action": "dropped_stale", "late_seconds": late}
            if log:
                log.warning(
                    "oneshot DROPPED (stale): %s was due %s, %ss late (> %ss cutoff)",
                    entry["id"], entry.get("fire_at_iso"), late, stale_cutoff)
            results.append(record)
            tick()
            continue

        returncode = runner(entry["command"])
        record = {"id": entry["id"], "label": entry.get("label"),
                  "action": "fired", "late_seconds": late, "returncode": returncode}
        if log:
            log.info("oneshot FIRED: %s (%ss late, rc=%s)",
                     entry["id"], late, returncode)
        results.append(record)
        tick()

    return results


def replay(now: float | None = None, directory: Path | None = None,
           runner=None, log=None, progress=None) -> dict:
    """Startup pass. Re-reads the spool the previous container left behind,
    reports what is still pending, and fires (or drops) whatever came due
    while the process was down.

    There is no external timer to re-arm here — being in the spool IS being
    armed, because run_due() polls it. What this function adds over the plain
    tick is the boot-time accounting: a missed deadline gets an explicit
    decision and a log line instead of quietly firing as if nothing happened.
    """
    now = time.time() if now is None else now
    directory = spool_dir() if directory is None else directory

    pending_before = load_entries(directory)
    fired = run_due(now=now, directory=directory, runner=runner, log=log,
                    progress=progress)

    still_pending = [e for e in pending_before if e["fire_at"] > now]
    if log:
        for entry in still_pending:
            log.info("oneshot RE-ARMED: %s fires at %s",
                     entry["id"], entry.get("fire_at_iso"))
        log.info("oneshot replay: %d re-armed, %d fired (missed deadline), "
                 "%d dropped (stale)",
                 len(still_pending),
                 sum(1 for r in fired if r["action"] == "fired"),
                 sum(1 for r in fired if r["action"] == "dropped_stale"))

    return {
        "rearmed": [e["id"] for e in still_pending],
        "fired": [r for r in fired if r["action"] == "fired"],
        "dropped": [r for r in fired if r["action"] == "dropped_stale"],
    }


# =============================================================================
# CLI
# =============================================================================

def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schedule arbitrary future work.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sched = sub.add_parser("schedule", help="spool a future command")
    p_sched.add_argument("--label", required=True)
    p_sched.add_argument("--when", required=True,
                         help="'10m', '+2h', '1h30m', or '2026-08-09 09:00'")
    p_sched.add_argument("--command")
    p_sched.add_argument("--message", help="deliver this as an unprompted message")
    p_sched.add_argument("--agent")
    p_sched.add_argument("--reply-channel")
    p_sched.add_argument("--replace", action="store_true")

    sub.add_parser("list", help="show pending oneshots")

    p_cancel = sub.add_parser("cancel", help="cancel pending oneshots")
    p_cancel.add_argument("labels", nargs="+")

    sub.add_parser("run-due", help="fire everything due now (one pass)")
    sub.add_parser("replay", help="startup pass: re-arm pending, fire missed")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "schedule":
            entry = schedule(
                label=args.label, when=args.when, command=args.command,
                message=args.message, agent=args.agent,
                reply_channel=args.reply_channel, replace=args.replace,
            )
            if args.json:
                print(json.dumps(_public(entry)))
            else:
                print(f"Scheduled {entry['id']} for {entry['fire_at_iso']} "
                      f"(fire_at={entry['fire_at']})")
            return 0

        if args.cmd == "list":
            entries = [_public(e) for e in load_entries()]
            if args.json:
                print(json.dumps({"pending": entries}))
            elif not entries:
                print("No pending oneshots.")
            else:
                for entry in entries:
                    print(f"{entry['id']}\t{entry['fire_at_iso']}\t{entry['command'][:80]}")
            return 0

        if args.cmd == "cancel":
            result = cancel(args.labels)
            if args.json:
                print(json.dumps(result))
            else:
                for cid in result["cancelled"]:
                    print(f"cancelled: {cid}")
                for miss in result["not_found"]:
                    print(f"not pending: {miss} (nothing to cancel)")
            return 0 if result["cancelled"] or not result["not_found"] else 1

        if args.cmd == "run-due":
            results = run_due()
            print(json.dumps({"results": results}) if args.json
                  else f"{len(results)} oneshot(s) processed")
            return 0

        if args.cmd == "replay":
            result = replay()
            print(json.dumps(result) if args.json
                  else f"re-armed {len(result['rearmed'])}, "
                       f"fired {len(result['fired'])}, "
                       f"dropped {len(result['dropped'])}")
            return 0
    except OneshotError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
