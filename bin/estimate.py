#!/usr/bin/env python3
"""Build-time estimates from finished builds in the build queue (#242).

  estimate.py calibrate [--dry-run]   write data/estimates/calibration.json
  estimate.py show                    print the calibration (computed fresh if none is stored)
  estimate.py for KIND [--builds N]   range for N builds of KIND (default 1)

Reads data/build-queue.db read-only; the only file it writes is calibration.json.
`--workspace DIR` (default $WORKSPACE_ROOT or /workspace); `--json` for tools.
Exit 0 on success, 1 when `for` has too little data to quote.
"""
from __future__ import annotations

import argparse
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import estimate as est  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", default=workspace_root())
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("show")
    f = sub.add_parser("for")
    f.add_argument("kind")
    f.add_argument("--builds", type=int, default=1)
    args = ap.parse_args(argv)

    data = Path(args.workspace) / "data"
    db, out = data / "build-queue.db", data / "estimates" / "calibration.json"

    if args.cmd == "calibrate":
        cal = est.calibrate(db)
        if not args.dry_run:
            est.write(cal, out)
        print(json.dumps(cal, indent=2) if args.json else _table(cal, written=None if args.dry_run else out))
        return 0
    cal = est.calibrate(db)
    if args.cmd == "show":
        print(json.dumps(cal, indent=2) if args.json else _table(cal))
        return 0
    try:
        r = est.estimate_for(cal, args.kind, args.builds)
    except ValueError as e:
        print(f"estimate: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(r, indent=2))
    elif r["quotable"]:
        print(f"{r['builds']} x {r['kind']}: {r['low_min']} to {r['high_min']} minutes "
              f"(median to 80th percentile of {r['n']} finished builds)")
    else:
        print(f"no estimate for {r['kind']}: {r['reason']}")
    return 0 if r["quotable"] else 1


def _table(cal, written=None) -> str:
    lines = [f"calibration {cal['generated_at']} (needs {cal['min_samples']} finished builds per kind)"]
    if not cal["kinds"]:
        lines.append("  no finished builds on record")
    for kind, e in cal["kinds"].items():
        lines.append(f"  {kind}: n={e['n']}, median {e['median_min']} min, p80 {e['p80_min']} min"
                     if e["enough"] else f"  {kind}: n={e['n']}, too few to quote")
    if written:
        lines.append(f"written: {written}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
