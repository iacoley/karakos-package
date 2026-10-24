"""Scheduled job: hourly friction scan (no LLM). Registered in lib/job_registry.py."""
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
from paths import workspace_root  # noqa: E402
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root

from lib import friction  # noqa: E402


def run_scheduled() -> None:
    ws = workspace_root()
    friction.scan(ws)
    friction.write_summary(ws)
