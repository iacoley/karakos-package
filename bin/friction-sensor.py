#!/usr/bin/env python3
"""Scan agent streams for friction (tool errors, retries, denials, rate limits).

Usage: friction-sensor.py [--summary] [--days N] [--workspace DIR]
Appends to data/friction-log.jsonl and refreshes data/friction/weekly.md.
--summary only prints the summary and scans nothing. Read-only on the logs.
"""
import argparse
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import friction  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--workspace", default=workspace_root())
    a = ap.parse_args(argv)
    if a.summary:
        print(friction.summary_text(friction.read_log(a.workspace, a.days), a.days), end="")
        return 0
    r = friction.scan(a.workspace)
    friction.write_summary(a.workspace, a.days)
    print(f"friction-sensor: scanned {r['scanned']} streams, {r['events']} new events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
