#!/usr/bin/env python3
"""
Deferred-message flusher — re-fire inbound messages that could not be
delivered to the agent server (#88).

When the agent server is down or rate-limited, `relay.py` and `poke.sh` spool
the exact `/message` payload they were about to POST into
`data/deferred-messages/*.json` instead of dropping it. This script re-POSTs
each spooled payload and is run by the scheduler every 5 minutes, so a
message sent during an outage arrives once the server is back — without the
user resending anything.

Refires are idempotent: the agent server answers 202 "duplicate" for a
message_id it has already queued, so a payload whose first POST actually
landed (but whose response was lost) cannot double-deliver.

Per file:
  - older than FLUSH_MAX_AGE_HOURS  -> moved to stale/    (never fired late)
  - unparseable                     -> moved to invalid/  (kept for inspection)
  - 202                             -> delivered, deleted
  - 429 / 5xx / connection error    -> left in place for the next cycle
  - any other 4xx                   -> moved to invalid/  (a refire cannot fix
                                       a payload the server rejects outright)

stale/ and invalid/ are pruned after FLUSH_RETENTION_DAYS.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or os.environ.get("KARAKOS_HOME") or os.path.abspath(os.path.join(__file__, "..", "..")))
DEFERRED_DIR = WORKSPACE_ROOT / "data" / "deferred-messages"
AGENT_SERVER_PORT = os.environ.get("AGENT_SERVER_PORT", "18791")
AGENT_SERVER_URL = os.environ.get(
    "AGENT_SERVER_URL", f"http://localhost:{AGENT_SERVER_PORT}"
)
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
MAX_AGE_HOURS = float(os.environ.get("FLUSH_MAX_AGE_HOURS", "24"))
RETENTION_DAYS = float(os.environ.get("FLUSH_RETENTION_DAYS", "7"))
POST_TIMEOUT_SECONDS = 10


def _post(payload: dict):
    """POST one payload to /message. Returns the HTTP status, 0 on no-connect."""
    req = urllib.request.Request(
        f"{AGENT_SERVER_URL}/message",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {AGENT_SERVER_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT_SECONDS) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def _move_aside(path: Path, bucket: str) -> None:
    dest_dir = path.parent / bucket
    dest_dir.mkdir(parents=True, exist_ok=True)
    path.rename(dest_dir / path.name)


def flush(now: float = None) -> dict:
    """One pass over the spool. Returns counters for logging and tests."""
    now = now if now is not None else time.time()
    summary = {"fired": 0, "kept": 0, "stale": 0, "invalid": 0, "pruned": 0}

    if DEFERRED_DIR.is_dir():
        for path in sorted(DEFERRED_DIR.glob("*.json")):
            if now - path.stat().st_mtime > MAX_AGE_HOURS * 3600:
                # Firing a day-old message resurfaces a conversation the user
                # has long since routed around; age it out instead.
                _move_aside(path, "stale")
                summary["stale"] += 1
                continue

            try:
                payload = json.loads(path.read_text())
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
            except (ValueError, OSError):
                # Fails identically on every future pass — retrying buys
                # nothing and costs a log line per cycle until stale-out.
                _move_aside(path, "invalid")
                summary["invalid"] += 1
                continue

            status = _post(payload)
            if status == 202:
                path.unlink()
                summary["fired"] += 1
            elif status == 429 or status >= 500 or status == 0:
                summary["kept"] += 1
            else:
                _move_aside(path, "invalid")
                summary["invalid"] += 1

    # Prune the inspection buckets; the scheduler log is the durable trail.
    for bucket in ("stale", "invalid"):
        bucket_dir = DEFERRED_DIR / bucket
        if not bucket_dir.is_dir():
            continue
        for path in bucket_dir.glob("*.json"):
            if now - path.stat().st_mtime > RETENTION_DAYS * 86400:
                path.unlink()
                summary["pruned"] += 1

    return summary


if __name__ == "__main__":
    result = flush()
    print(json.dumps(result))
    sys.exit(0)
