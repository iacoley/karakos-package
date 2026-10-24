#!/usr/bin/env python3
"""
Session Summarizer — Generates session summaries for agent context preservation

Reads recent agent stream logs, calls Claude to generate a summary, validates
required headers, and outputs to checkpoint file for next session re-injection.

NOTE (step 2.6): the agent server no longer calls this script. A session now
hands itself over through a handoff note written by the session that holds the
context (lib/session_policy.py). This stays in the tree for one release as a
manual tool and will be removed after that.
"""

import argparse
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime

WORKSPACE_ROOT = Path(workspace_root())
STREAM_LOG_DIR = WORKSPACE_ROOT / "logs" / "agent-streams"
SUMMARY_DIR = WORKSPACE_ROOT / "logs" / "session-summaries"
SUMMARY_FILE_TEMPLATE = WORKSPACE_ROOT / "data" / "last-session-summary-{agent}.md"
AUDIT_LOG = WORKSPACE_ROOT / "logs" / "summarizer-audit.jsonl"

# How many rotated stream logs to walk back through. agent-server rolls to a
# new file per boot, per day and at a size cap, so the newest file can be a
# handful of events old — reading only it would summarize the last 30
# seconds of a six-hour session.
STREAM_FILE_LOOKBACK = 3

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

def extract_assistant_text(event: dict) -> str:
    """Concatenate the text blocks of one stream-json `assistant` event.

    Claude Code emits `assistant` events whose `message.content` is a LIST OF
    BLOCKS (thinking / text / tool_use) — never a flat {"type": "text"}
    record. read_agent_response() in bin/agent-server.py is the reference
    parser; this has to agree with it.
    """
    if event.get("type") != "assistant":
        return ""
    message = event.get("message") or {}
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def format_stream_event(event: dict) -> list:
    """Render one stream-json event as lines for the summarizer prompt.

    Returns [] for events with nothing worth summarizing (thinking blocks,
    tool results, init/result bookkeeping).
    """
    event_type = event.get("type", "")
    lines = []

    if event_type == "assistant":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    lines.append(f"[TEXT] {text[:200]}")
            elif btype == "tool_use":
                lines.append(f"[TOOL] {block.get('name') or 'unknown'}")

    elif event_type == "user":
        # The prompt the agent was answering. Only the plain-string form —
        # the block-list form is tool_result payloads, which are bulky and
        # say nothing a [TOOL] line hasn't already said.
        message = event.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            lines.append(f"[USER] {content.strip()[:200]}")

    # Flat shapes, kept so a log written by anything other than the tee in
    # bin/agent-server.py still parses.
    elif event_type == "text":
        text = (event.get("text") or "").strip()
        if text:
            lines.append(f"[TEXT] {text[:200]}")
    elif event_type == "tool_use":
        lines.append(f"[TOOL] {event.get('name') or 'unknown'}")

    return lines


def read_recent_stream(agent: str, limit: int = 50) -> str:
    """Read last N lines from agent stream logs"""
    # Find most recent stream log for agent
    if not STREAM_LOG_DIR.exists():
        return ""
    stream_files = sorted(STREAM_LOG_DIR.glob(f"{agent}_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not stream_files:
        return ""

    # Read the last N lines, walking back through rotated files if the
    # newest one is shorter than that.
    lines = []
    for path in stream_files[:STREAM_FILE_LOOKBACK]:
        need = limit - len(lines)
        if need <= 0:
            break
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()[-need:] + lines
        except OSError:
            continue

    # Parse and format
    formatted = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            formatted.extend(format_stream_event(event))

    return "\n".join(formatted)

def call_summarizer(stream_content: str) -> tuple[bool, str, dict]:
    """Call Claude to generate summary"""
    prompt = SUMMARIZER_PROMPT.format(stream_content=stream_content)

    cmd = [
        "claude", "-p", prompt,
        "--model", "sonnet",
        "--max-turns", "1",
        "--output-format", "stream-json",
        # The CLI refuses stream-json output under -p without it.
        "--verbose",
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

        # Parse stream-json output — same block shape as the agent stream.
        summary = ""
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            summary += extract_assistant_text(event)
            # Fallback for a build that only emits the flat result string.
            if not summary and event.get("type") == "result":
                summary = event.get("result") or ""

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
    checkpoint_path = Path(str(SUMMARY_FILE_TEMPLATE).format(agent=agent))
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
