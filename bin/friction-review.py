#!/usr/bin/env python3
"""Group recurring friction into causes and draft one issue per cause.

Usage: friction-review.py [--days N] [--min-count N] [--json]
                          [--file --repo OWNER/NAME --yes]
Writes data/friction/YYYY-MM-DD.md. Filing upstream only happens with
--file AND --yes (a person approved the drafts), once per cause.
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

from lib import friction_review as fr  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-count", type=int, default=fr.MIN_COUNT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--file", action="store_true", help="file the drafts as issues")
    ap.add_argument("--repo", default="")
    ap.add_argument("--yes", action="store_true", help="the drafts were approved by a person")
    ap.add_argument("--workspace", default=workspace_root(ROOT))
    a = ap.parse_args(argv)
    r = fr.review(a.workspace, a.days, a.min_count)
    out = {"report": r["report"], "events": r["events"], "causes": len(r["causes"])}
    if a.file:
        if not a.repo:
            ap.error("--file needs --repo OWNER/NAME")
        if not a.yes:
            print("not filing: drafts need approval, rerun with --yes after review", file=sys.stderr)
            return 2
        out["filed"] = fr.file_issues(a.workspace, r["causes"], a.repo, approved=True)
    print(json.dumps(out) if a.json else
          f"friction-review: {out['events']} events, {out['causes']} recurring cause(s), report {out['report']}"
          + (f", filed {len(out['filed'])}" if "filed" in out else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
