#!/usr/bin/env python3
"""
Data Purging — Clean up old logs and messages.

Purges:
1. Message JSONL files older than MESSAGE_RETENTION_DAYS (default 90)
2. Tool audit records older than TOOL_AUDIT_RETENTION_DAYS (default 30)
3. Session summary archives (keeps last 30 per agent)
4. Agent stream logs older than STREAM_LOG_RETENTION_DAYS (default 7)

Called by scheduler daily at 4:30 AM.
"""

import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MESSAGES_DIR = WORKSPACE / "data" / "messages"
# Must match mcp/tools-server.py:AUDIT_DB_PATH — that server is the only
# writer, and this purge is the only reader. tests/test_purge.py asserts the
# two constants are equal rather than re-stating the literal.
TOOL_AUDIT_DB = WORKSPACE / "data" / "mcp-tools-audit.db"
OUTBOX_DB = WORKSPACE / "data" / "outbox" / "outbox.db"
SESSION_SUMMARIES_DIR = WORKSPACE / "logs" / "session-summaries"

MESSAGE_RETENTION_DAYS = int(os.environ.get("MESSAGE_RETENTION_DAYS", "90"))
TOOL_AUDIT_RETENTION_DAYS = int(os.environ.get("TOOL_AUDIT_RETENTION_DAYS", "30"))
SUMMARY_RETENTION_COUNT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] purge-data: %(message)s",
)
log = logging.getLogger(__name__)


def purge_old_messages() -> int:
    """Delete message JSONL files older than retention period."""
    if not MESSAGES_DIR.exists():
        return 0

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=MESSAGE_RETENTION_DAYS)
    deleted = 0

    for file in MESSAGES_DIR.glob("messages-*.jsonl"):
        try:
            # Extract date from filename: messages-YYYY-MM-DD.jsonl
            date_str = file.stem.replace("messages-", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

            if file_date < cutoff_date:
                file.unlink()
                deleted += 1
                log.info(f"Deleted old message file: {file.name}")
        except (ValueError, OSError) as e:
            log.warning(f"Failed to process {file.name}: {e}")
            continue

    if deleted:
        log.info(f"Purged {deleted} message files older than {MESSAGE_RETENTION_DAYS} days")
    return deleted


def purge_tool_audit() -> int:
    """Delete tool audit records older than retention period and run VACUUM."""
    if not TOOL_AUDIT_DB.exists():
        return 0

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=TOOL_AUDIT_RETENTION_DAYS)
    cutoff_str = cutoff_date.isoformat()

    try:
        conn = sqlite3.connect(str(TOOL_AUDIT_DB))
        cursor = conn.execute(
            "DELETE FROM tool_calls WHERE timestamp < ?",
            (cutoff_str,)
        )
        deleted = cursor.rowcount
        conn.commit()

        # Run VACUUM to reclaim space
        conn.execute("VACUUM")
        conn.close()

        if deleted:
            log.info(f"Purged {deleted} tool audit records older than {TOOL_AUDIT_RETENTION_DAYS} days")
        return deleted

    except sqlite3.Error as e:
        log.error(f"Failed to purge tool audit: {e}")
        return 0


_SUMMARY_TS_RE = re.compile(r"^(?P<agent>.+)-\d{4}-\d{2}-\d{2}-\d{6}$")
_SUMMARY_LEGACY_RE = re.compile(r"^(?P<agent>.+?)-\d[\d-]*$")


def _summary_agent(stem: str):
    """Agent name from '{agent}-{YYYY-MM-DD-HHMMSS}' (bin/summarize-session.py).

    Strips the trailing timestamp rather than splitting on the first hyphen, so
    hyphenated agent names ('test-agent') keep their own retention budget.
    """
    m = _SUMMARY_TS_RE.match(stem) or _SUMMARY_LEGACY_RE.match(stem)
    return m.group("agent") if m else None


def purge_old_session_summaries() -> int:
    """Keep only the last N session summaries per agent."""
    if not SESSION_SUMMARIES_DIR.exists():
        return 0

    # Group summaries by agent name (extract from filename pattern)
    agent_summaries = {}

    for file in SESSION_SUMMARIES_DIR.glob("*.md"):
        agent = _summary_agent(file.stem)
        if agent:
            agent_summaries.setdefault(agent, []).append(file)

    deleted = 0
    for agent, files in agent_summaries.items():
        # Sort by modification time (newest first)
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        # Delete all but the last N
        to_delete = files[SUMMARY_RETENTION_COUNT:]
        for file in to_delete:
            try:
                file.unlink()
                deleted += 1
            except OSError as e:
                log.warning(f"Failed to delete {file.name}: {e}")

    if deleted:
        log.info(f"Purged {deleted} old session summaries (kept {SUMMARY_RETENTION_COUNT} per agent)")
    return deleted


# Raw stream-json tee written by bin/agent-server.py (#148). Only the newest
# few files per agent are ever read — bin/summarize-session.py tails them for
# the session summary — so these expire by age, and short (a week) because
# they are verbatim transcript volume, not a record anything reads later.
AGENT_STREAMS_DIR = WORKSPACE / "logs" / "agent-streams"
STREAM_LOG_RETENTION_DAYS = int(os.environ.get("STREAM_LOG_RETENTION_DAYS", "7"))


def purge_old_stream_logs() -> int:
    """Delete agent stream logs whose last write is past the retention window.

    By mtime, not by the timestamp in the filename: agent-server keeps a log
    open across a whole boot, so the name says when it was opened and only
    mtime says when it stopped being the live one.
    """
    if not AGENT_STREAMS_DIR.exists():
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=STREAM_LOG_RETENTION_DAYS)).timestamp()
    deleted = 0

    for file in AGENT_STREAMS_DIR.glob("*.jsonl"):
        try:
            if file.stat().st_mtime < cutoff:
                file.unlink()
                deleted += 1
        except OSError as e:
            log.warning(f"Failed to delete {file.name}: {e}")
            continue

    if deleted:
        log.info(f"Purged {deleted} agent stream logs older than {STREAM_LOG_RETENTION_DAYS} days")
    return deleted


def purge_outbox() -> int:
    """Delivered outbox rows after 7 days, dead ones after 90 (events with them)."""
    if not OUTBOX_DB.is_file():
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import outbox
    conn = outbox.open_store(OUTBOX_DB)
    try:
        res = outbox.purge(conn)
    finally:
        conn.close()
    if res["purged"]:
        log.info(f"Purged {res['purged']} outbox row(s): {res}")
    return res["purged"]


def main():
    log.info("Data purge starting")

    try:
        stats = {
            "messages": purge_old_messages(),
            "tool_audit": purge_tool_audit(),
            "session_summaries": purge_old_session_summaries(),
            "stream_logs": purge_old_stream_logs(),
            "outbox": purge_outbox(),
        }

        log.info(f"Purge complete: {stats}")

    except Exception as e:
        log.error(f"Purge failed: {e}")
        raise


if __name__ == "__main__":
    main()
