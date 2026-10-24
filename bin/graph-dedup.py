#!/usr/bin/env python3
"""Find and merge duplicate graph entities by embedding similarity.

Usage: graph-dedup.py [--live] [--threshold 0.92] [--max 50] [--json] [--workspace DIR]
Dry run by default: prints the pairs it would merge. --live merges at most 50
per run (soft: the merged-away entity is archived and its name still resolves).
Needs entity embeddings (the nightly memory-consolidate job backfills them).
"""
import argparse
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.graph import entity_dedup  # noqa: E402
from lib.graph.store import GraphNotInitialised, open_graph  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="merge instead of only listing")
    ap.add_argument("--threshold", type=float, default=entity_dedup.THRESHOLD)
    ap.add_argument("--max", type=int, default=entity_dedup.MAX_MERGES, dest="cap")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workspace", default=workspace_root())
    a = ap.parse_args(argv)
    if not 0.5 <= a.threshold <= 1.0:
        ap.error("--threshold must be between 0.5 and 1.0")
    try:
        store = open_graph(Path(a.workspace) / "data")
    except GraphNotInitialised as e:
        print(f"graph-dedup: {e}", file=sys.stderr)
        return 1
    r = entity_dedup.run(store, live=a.live, threshold=a.threshold, max_merges=a.cap)
    if a.json:
        print(json.dumps(r))
        return 0
    verb = "merged" if a.live else "would merge"
    for p in r["pairs"]:
        print(f"{verb} {p['from_name']!r} -> {p['into_name']!r} ({p['kind']}, cos {p['score']})")
    for p in r["report_only"]:
        print(f"review only ({p['why']}) {p['a_name']!r} | {p['b_name']!r} ({p['kind']}, cos {p['score']})")
    tail = " (capped)" if r["capped"] else ""
    print(f"{r['candidates']} candidate pair(s); {r['merged']} merged{tail}; "
          f"{r['report_only_count']} listed for review, never merged"
          + ("" if a.live else "; dry run, use --live to merge"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
