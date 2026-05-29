#!/usr/bin/env python3
"""
Session Summarizer — Generates session summaries for agent context preservation

Reads recent agent stream logs, calls Claude to generate a summary, validates
required headers, and outputs to checkpoint file for next session re-injection.
"""

import argparse
import json
import os
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime

# Backend selection — "claude" (default) shells out to the Claude CLI; "ollama"
# imports the minimal native runner from the sibling bin/ directory.
AGENT_BACKEND = os.environ.get("AGENT_BACKEND", "claude")
sys.path.insert(0, str(Path(__file__).resolve().parent))

WORKSPACE_ROOT = Path("/workspace")
STREAM_LOG_DIR = WORKSPACE_ROOT / "logs" / "agent-streams"
SUMMARY_DIR = WORKSPACE_ROOT / "logs" / "session-summaries"
LAST_SUMMARY_TEMPLATE = WORKSPACE_ROOT / "data" / "last-session-summary-{agent}.md"
AUDIT_LOG = WORKSPACE_ROOT / "logs" / "summarizer-audit.jsonl"

REQUIRED_HEADERS = [
    "## Primary Task",
    "## Current State",
    "## Key Context for Next Session"
]

SUMMARIZER_PROMPT = """You are a session summarizer. Your job is to read the recent agent activity stream and generate a concise summary that will be injected into the agent's next session to preserve context.

The summary must contain these sections:

## Primary Task
What is the agent currently working on? 1-2 sentences.

## Current State
Where did the agent leave off? What's the next step? 2-3 sentences.

## Key Context for Next Session
Critical information that must be preserved (decisions made, files changed, commitments, blockers). Bullet list, max 5 items.

Keep it concise — aim for 150-250 words total. Do not include full transcripts or code snippets.

Recent agent activity:
{stream_content}
"""

def read_recent_stream(agent: str, limit: int = 50) -> str:
    """Read last N lines from agent stream logs"""
    # Find most recent stream log for agent
    stream_files = sorted(STREAM_LOG_DIR.glob(f"{agent}_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not stream_files:
        return ""

    # Read last N lines
    lines = []
    with open(stream_files[0]) as f:
        all_lines = f.readlines()
        lines = all_lines[-limit:]

    # Parse and format
    formatted = []
    for line in lines:
        try:
            event = json.loads(line)
            event_type = event.get("type", "")

            if event_type == "text":
                formatted.append(f"[TEXT] {event.get('text', '')[:200]}")
            elif event_type == "tool_use":
                tool = event.get("name", "unknown")
                formatted.append(f"[TOOL] {tool}")
        except json.JSONDecodeError:
            continue

    return "\n".join(formatted)

def call_summarizer(stream_content: str) -> tuple[bool, str, dict]:
    """Call the configured backend to generate a summary"""
    prompt = SUMMARIZER_PROMPT.format(stream_content=stream_content)

    if AGENT_BACKEND == "ollama":
        from ollama_runner import run_oneshot
        ok, summary, meta = run_oneshot(prompt)
        if not ok:
            return False, "", meta
        summary = summary.strip()
        missing = [h for h in REQUIRED_HEADERS if h not in summary]
        if missing:
            return False, summary, {**meta, "error": "missing_headers", "missing": missing}
        return True, summary, meta

    cmd = [
        "claude", "-p", prompt,
        "--model", "sonnet",
        "--max-turns", "1",
        "--output-format", "stream-json"
    ]

    start_time = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20
        )

        duration_ms = (time.time() - start_time) * 1000

        if result.returncode != 0:
            return False, "", {"error": "subprocess_failed", "duration_ms": duration_ms}

        # Parse stream-json output
        summary = ""
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "text":
                    summary += event.get("text", "")
            except json.JSONDecodeError:
                continue

        summary = summary.strip()

        # Validate required headers
        missing = [h for h in REQUIRED_HEADERS if h not in summary]
        if missing:
            return False, summary, {"error": "missing_headers", "missing": missing, "duration_ms": duration_ms}

        return True, summary, {"duration_ms": duration_ms}

    except subprocess.TimeoutExpired:
        duration_ms = (time.time() - start_time) * 1000
        return False, "", {"error": "timeout", "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        return False, "", {"error": str(e), "duration_ms": duration_ms}

def save_summary(agent: str, summary: str):
    """Save summary to checkpoint file and timestamped archive"""
    # Create checkpoint (overwrites)
    checkpoint_path = Path(str(LAST_SUMMARY_TEMPLATE).format(agent=agent))
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with open(checkpoint_path, "w") as f:
        f.write(summary)

    # Create timestamped copy
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive_path = SUMMARY_DIR / f"{agent}-{timestamp}.md"
    with open(archive_path, "w") as f:
        f.write(summary)

def log_audit(event: str, agent: str, success: bool, metadata: dict):
    """Log to audit trail"""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now().isoformat(),
        "event": event,
        "agent": agent,
        "success": success,
        **metadata
    }

    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

def main():
    parser = argparse.ArgumentParser(description="Generate session summary for agent")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument("--limit", type=int, default=50, help="Number of stream lines to read")

    args = parser.parse_args()

    # Read recent stream
    stream_content = read_recent_stream(args.agent, args.limit)

    if not stream_content:
        print(f"No recent stream data for {args.agent}", file=sys.stderr)
        log_audit("summarize", args.agent, False, {"error": "no_stream_data"})
        sys.exit(1)

    # Generate summary
    success, summary, metadata = call_summarizer(stream_content)

    if not success:
        print(f"Failed to generate summary: {metadata.get('error')}", file=sys.stderr)
        log_audit("summarize", args.agent, False, metadata)
        sys.exit(1)

    # Save summary
    save_summary(args.agent, summary)

    log_audit("summarize", args.agent, True, metadata)
    print(f"Summary generated and saved for {args.agent}")

if __name__ == "__main__":
    main()
