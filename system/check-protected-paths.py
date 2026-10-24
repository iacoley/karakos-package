#!/usr/bin/env python3
"""
Protected Paths Checker — Verifies staged git files against protection rules.

Called by pre-commit hook. Prints blocked paths and exits non-zero if any
Tier 1 protected files are staged for commit.

Usage:
    check-protected-paths.py --staged
"""

import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

WORKSPACE = Path(workspace_root())
CONFIG_PATH = WORKSPACE / "config" / "protected-paths.json"
EVENTS_LOG = WORKSPACE / "logs" / "git-events.jsonl"


def load_config() -> dict:
    """Load protected paths configuration."""
    if not CONFIG_PATH.exists():
        return {"tier1_protected": [], "tier2_review_required": [], "unprotected_overrides": []}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_staged_files() -> list[str]:
    """Get list of staged files from git."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=str(WORKSPACE)
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return []


def is_override(path: str, overrides: list[str]) -> bool:
    """Check if path matches any unprotected override pattern."""
    for pattern in overrides:
        if fnmatch(path, pattern):
            return True
        # Check directory prefix patterns like "agents/*/persona/"
        if pattern.endswith("/") and path.startswith(pattern.replace("*", "")):
            return True
        # Handle glob patterns
        parts = pattern.split("/")
        path_parts = path.split("/")
        if len(path_parts) >= len(parts):
            match = True
            for i, p in enumerate(parts):
                if p == "*":
                    continue
                if p == "" and i == len(parts) - 1:
                    continue  # trailing slash
                if i < len(path_parts) and path_parts[i] != p:
                    match = False
                    break
            if match:
                return True
    return False


def check_tier1(path: str, protected: list[str]) -> bool:
    """Check if path matches any Tier 1 protected pattern."""
    for pattern in protected:
        if pattern.endswith("/"):
            if path.startswith(pattern) or path == pattern.rstrip("/"):
                return True
        elif path == pattern:
            return True
    return False


def check_tier2(path: str, review_required: list[str]) -> bool:
    """Check if path matches any Tier 2 review-required pattern."""
    for pattern in review_required:
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif path == pattern:
            return True
    return False


def main():
    if "--staged" not in sys.argv:
        print("Usage: check-protected-paths.py --staged")
        sys.exit(1)

    config = load_config()
    staged = get_staged_files()

    if not staged:
        sys.exit(0)

    tier1_blocked = []
    tier2_flagged = []

    for path in staged:
        # Check overrides first
        if is_override(path, config.get("unprotected_overrides", [])):
            continue

        if check_tier1(path, config.get("tier1_protected", [])):
            tier1_blocked.append(path)
        elif check_tier2(path, config.get("tier2_review_required", [])):
            tier2_flagged.append(path)

    # Log events asynchronously
    if tier1_blocked or tier2_flagged:
        try:
            from datetime import datetime, timezone
            EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(EVENTS_LOG, "a") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tier1_blocked": tier1_blocked,
                    "tier2_flagged": tier2_flagged,
                }) + "\n")
        except Exception:
            pass

    if tier1_blocked:
        print("BLOCKED — Tier 1 protected paths (owner approval required):")
        for path in tier1_blocked:
            print(f"  {path}")
        if tier2_flagged:
            print("\nTier 2 paths (review required):")
            for path in tier2_flagged:
                print(f"  {path}")
        # Output blocked paths for pre-commit hook
        print("\n".join(tier1_blocked))
        sys.exit(1)

    if tier2_flagged:
        print("WARNING — Tier 2 paths staged (reviewer approval recommended):")
        for path in tier2_flagged:
            print(f"  {path}")
        # Tier 2 doesn't block, just warns
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
