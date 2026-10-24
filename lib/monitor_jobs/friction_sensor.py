"""Scheduled job: hourly friction scan (no LLM). Registered in lib/job_registry.py."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root

from lib import friction  # noqa: E402


def run_scheduled() -> None:
    ws = os.environ.get("WORKSPACE_ROOT") or os.environ.get("KARAKOS_HOME") or os.path.abspath(os.path.join(__file__, "..", "..", ".."))
    friction.scan(ws)
    friction.write_summary(ws)
