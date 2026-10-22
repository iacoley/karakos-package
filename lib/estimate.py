"""Build-time calibration from the build queue (#242). Read-only on the queue.

Finished builds (`status='done'`, with both `started_at` and `finished_at`) give
a duration. Per `kind` we keep the count, median and 80th percentile in minutes.
Fewer than MIN_SAMPLES finished builds of a kind is not enough to quote: the
entry says so instead of inventing a number.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MIN_SAMPLES = 3
SCHEMA = 1


def _ts(value):
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def durations(db_path) -> dict:
    """{kind: [minutes, ...]} for finished builds. Opens the database read-only;
    a missing file or table is an empty result."""
    path = Path(db_path)
    if not path.exists():
        return {}
    out: dict = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute("SELECT kind, started_at, finished_at FROM build_queue "
                            "WHERE status='done' AND started_at IS NOT NULL "
                            "AND finished_at IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    for kind, start, end in rows:
        a, b = _ts(start), _ts(end)
        if a is not None and b is not None and b >= a:
            out.setdefault(kind or "unknown", []).append((b - a) / 60.0)
    return out


def _p80(sorted_vals):
    # nearest-rank, so a small sample never invents a value above its maximum
    k = max(0, min(len(sorted_vals) - 1, -(-80 * len(sorted_vals) // 100) - 1))
    return sorted_vals[k]


def calibrate(db_path, now=None) -> dict:
    kinds = {}
    for kind, vals in sorted(durations(db_path).items()):
        vals.sort()
        entry = {"n": len(vals), "enough": len(vals) >= MIN_SAMPLES}
        if entry["enough"]:
            entry.update(median_min=round(statistics.median(vals), 1),
                         p80_min=round(_p80(vals), 1))
        kinds[kind] = entry
    when = datetime.fromtimestamp(now if now is not None else datetime.now().timestamp(),
                                  timezone.utc)
    return {"schema": SCHEMA, "generated_at": when.isoformat(), "min_samples": MIN_SAMPLES,
            "kinds": kinds}


def write(cal: dict, out_path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".calibration-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cal, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        finally:
            raise
    return out


def estimate_for(cal: dict, kind: str, builds: int = 1) -> dict:
    """A range for `builds` builds of `kind`: median to 80th percentile, summed.
    `quotable` is False when the data is too thin; then no numbers are given."""
    if builds < 1:
        raise ValueError("builds must be at least 1")
    entry = (cal.get("kinds") or {}).get(kind)
    if not entry:
        return {"kind": kind, "builds": builds, "quotable": False, "n": 0,
                "reason": f"no finished {kind!r} builds on record"}
    if not entry.get("enough"):
        return {"kind": kind, "builds": builds, "quotable": False, "n": entry["n"],
                "reason": f"only {entry['n']} finished (need {cal.get('min_samples', MIN_SAMPLES)})"}
    return {"kind": kind, "builds": builds, "quotable": True, "n": entry["n"],
            "low_min": round(entry["median_min"] * builds, 1),
            "high_min": round(entry["p80_min"] * builds, 1)}
