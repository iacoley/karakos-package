#!/usr/bin/env python3
"""python3 bin/graph-consolidate.py [--dry-run] [--workspace DIR]

Runs the nightly graph consolidation once. --dry-run computes every pass's
counts inside a rolled-back transaction, calls no model and prints them.
"""
import argparse
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.monitor_jobs import memory_consolidate  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workspace", default=workspace_root())
    args = ap.parse_args(argv)
    try:
        stats = memory_consolidate.run_job({"workspace": args.workspace, "dry_run": args.dry_run})
    except Exception as e:
        print(f"consolidation failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
