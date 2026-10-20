"""Monitor job: nightly graph consolidation (replaces bin/memory-maintenance.py).

Registered in lib/job_registry.py as `memory-consolidate`; the scheduler calls
`run_scheduled`. The job writes data/health/memory-consolidate.json on success
and on failure so the health monitor can tell a dead job from a failing one.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root, for lib.graph

from lib.graph import consolidate, entity_dedup  # noqa: E402
from lib.graph.store import open_graph  # noqa: E402

HEALTH_NAME = "memory-consolidate"
HEALTH_FILE = "memory-consolidate.json"


def _workspace(ctx) -> Path:
    ws = ctx.get("workspace") if isinstance(ctx, dict) else getattr(ctx, "workspace", None)
    return Path(ws or os.environ.get("WORKSPACE_ROOT", "/workspace"))


def _write_health(workspace: Path, ok: bool, stats: dict) -> None:
    path = workspace / "data" / "health" / HEALTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy" if ok else "error",
        "stats": stats,
    }))
    os.replace(tmp, path)


def _entity_dedup_pass(store, stats, now=None) -> None:
    """Optional weekly (Sunday) entity dedup: MEMORY_ENTITY_DEDUP=report lists
    pairs, =live merges up to 50. Unset or anything else: off."""
    mode = os.environ.get("MEMORY_ENTITY_DEDUP", "").strip().lower()
    if mode not in ("report", "live"):
        return
    when = now if hasattr(now, "weekday") else datetime.now(timezone.utc)
    if when.weekday() != 6:
        return
    try:
        stats["entity_dedup"] = entity_dedup.run(store, live=(mode == "live"))
    except Exception as e:  # noqa: BLE001 - an optional pass never fails the job
        stats["entity_dedup"] = {"error": str(e)}


def run_job(ctx) -> dict:
    """ctx: object or dict with `workspace` (and optional `dry_run`, `score_fn`,
    `now`). Returns the stats dict; raises after writing the failure heartbeat."""
    ws = _workspace(ctx)
    get = (lambda k, d=None: ctx.get(k, d)) if isinstance(ctx, dict) \
        else (lambda k, d=None: getattr(ctx, k, d))
    started = time.time()
    try:
        store = open_graph(ws / "data")
        stats = consolidate.run(store, now=get("now"), messages_dir=ws / "data" / "messages",
                                score_fn=get("score_fn"), dry_run=bool(get("dry_run", False)))
        _entity_dedup_pass(store, stats, get("now"))
        stats["duration_s"] = round(time.time() - started, 2)
        if not get("dry_run", False):
            _write_health(ws, not stats["errors"],
                          stats if not stats["errors"] else {**stats, "error": json.dumps(stats["errors"])})
        return stats
    except Exception as e:
        _write_health(ws, False, {"error": str(e)})
        raise


def run_scheduled() -> None:
    """Scheduler entry point (job_registry run target)."""
    run_job({"workspace": os.environ.get("WORKSPACE_ROOT", "/workspace")})


JOB = {
    "name": "memory-consolidate",
    "schedule": "0 3 * * *",
    "run": run_job,
    "timeout_s": 1800,
    "heartbeat": "memory-consolidate",
}
