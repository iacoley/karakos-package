"""Scheduled job: nightly friction review (report only; never files). No LLM."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root

from lib import friction_review  # noqa: E402


def run_scheduled() -> None:
    friction_review.review(os.environ.get("WORKSPACE_ROOT", "/workspace"))
