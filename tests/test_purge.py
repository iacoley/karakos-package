"""
Tests for bin/purge-data.py — Data retention enforcement.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT


class TestPurgeMessages:
    """Test JSONL message file purging."""

    def _make_purger(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MESSAGE_RETENTION_DAYS", "30")
        return import_script("purge-data")

    def test_deletes_old_message_files(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        msgs_dir = tmp_workspace / "data" / "messages"

        # Recent file is dated relative to now so it always falls inside the
        # 30-day retention window. A hardcoded date here becomes a time bomb:
        # once wall-clock passes date+30d the file is correctly purged and the
        # assertion below breaks (regressed CI on 2026-05-11).
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")

        (msgs_dir / "messages-2026-01-01.jsonl").write_text('{"test": true}\n')
        (msgs_dir / "messages-2026-02-01.jsonl").write_text('{"test": true}\n')
        (msgs_dir / f"messages-{recent}.jsonl").write_text('{"test": true}\n')

        deleted = purge.purge_old_messages()

        assert not (msgs_dir / "messages-2026-01-01.jsonl").exists()
        assert not (msgs_dir / "messages-2026-02-01.jsonl").exists()
        assert (msgs_dir / f"messages-{recent}.jsonl").exists()
        assert deleted >= 2

    def test_keeps_recent_message_files(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        msgs_dir = tmp_workspace / "data" / "messages"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (msgs_dir / f"messages-{today}.jsonl").write_text('{"test": true}\n')

        deleted = purge.purge_old_messages()
        assert deleted == 0
        assert (msgs_dir / f"messages-{today}.jsonl").exists()

    def test_handles_empty_directory(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        deleted = purge.purge_old_messages()
        assert deleted == 0

    def test_ignores_non_message_files(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        msgs_dir = tmp_workspace / "data" / "messages"

        (msgs_dir / "something-else.jsonl").write_text('{"test": true}\n')

        deleted = purge.purge_old_messages()
        assert deleted == 0
        assert (msgs_dir / "something-else.jsonl").exists()


class TestPurgeSessionSummaries:
    """Test session summary retention."""

    def _make_purger(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        purge = import_script("purge-data")
        purge.SUMMARY_RETENTION_COUNT = 3
        return purge

    def test_keeps_last_n_summaries_per_agent(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        summaries_dir = tmp_workspace / "logs" / "session-summaries"

        for i in range(5):
            f = summaries_dir / f"agent1-2026040{i}.md"
            f.write_text(f"Summary {i}")
            os.utime(f, (time.time() - (5 - i) * 3600, time.time() - (5 - i) * 3600))

        deleted = purge.purge_old_session_summaries()
        assert deleted == 2

        remaining = list(summaries_dir.glob("agent1-*.md"))
        assert len(remaining) == 3

    def test_hyphenated_agent_names_get_their_own_budget(self, tmp_workspace, monkeypatch):
        """Refs #156 (purge-data: hyphenated agent names share one budget)."""
        purge = self._make_purger(tmp_workspace, monkeypatch)
        summaries_dir = tmp_workspace / "logs" / "session-summaries"
        for agent in ("test-agent", "test-other"):
            for i in range(4):
                f = summaries_dir / f"{agent}-2026-10-0{i + 1}-120000.md"
                f.write_text("x")
                t = time.time() - (4 - i) * 3600
                os.utime(f, (t, t))
        assert purge.purge_old_session_summaries() == 2
        assert len(list(summaries_dir.glob("test-agent-*.md"))) == 3
        assert len(list(summaries_dir.glob("test-other-*.md"))) == 3

    def test_summary_agent_strips_trailing_timestamp(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        assert purge._summary_agent("test-agent-2026-10-09-120000") == "test-agent"
        assert purge._summary_agent("amos-2026-10-09-120000") == "amos"

    def test_handles_empty_directory(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        deleted = purge.purge_old_session_summaries()
        assert deleted == 0


class TestPurgeToolAudit:
    """Test tool-audit retention against the database the tool server writes.

    These are the tests that were missing: nothing here ever exercised
    ``purge_tool_audit``, so ``TOOL_AUDIT_DB`` pointed at ``mcp/tool-audit.db``
    — a path no writer in the repo has ever created — for as long as the file
    has existed, and ``TOOL_AUDIT_RETENTION_DAYS`` was a no-op (#150).

    The guard is deliberately *not* another string literal: asserting
    ``purge.TOOL_AUDIT_DB == tools_server.AUDIT_DB_PATH`` fails whenever reader
    and writer drift apart, which a duplicated literal cannot do.
    """

    def _modules(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("TOOL_AUDIT_RETENTION_DAYS", "30")
        purge = import_script("purge-data")
        tools_server = import_script(
            "tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py"
        )
        return purge, tools_server

    def test_purge_path_matches_the_writer(self, tmp_workspace, monkeypatch):
        purge, tools_server = self._modules(tmp_workspace, monkeypatch)
        assert purge.TOOL_AUDIT_DB == tools_server.AUDIT_DB_PATH

    def test_purge_path_is_one_something_actually_writes(self, tmp_workspace, monkeypatch):
        """Let the real writer create its database, then look for it.

        A purge aimed at a path nothing writes fails here even if the two
        constants above were changed in lockstep to a third wrong value.
        """
        purge, tools_server = self._modules(tmp_workspace, monkeypatch)

        conn = tools_server.init_audit_db()
        tools_server.log_audit(conn, "sysmon", '{}', 12, 3.4, True)
        conn.close()

        assert purge.TOOL_AUDIT_DB.exists(), (
            f"purge targets {purge.TOOL_AUDIT_DB}, which the tool server "
            f"did not create"
        )

    def test_deletes_rows_past_retention_and_keeps_recent(self, tmp_workspace, monkeypatch):
        purge, tools_server = self._modules(tmp_workspace, monkeypatch)

        # Schema comes from the writer, so the column purge_tool_audit filters
        # on ("timestamp") is the one tools-server actually creates.
        conn = tools_server.init_audit_db()
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.executemany(
            "INSERT INTO tool_calls (timestamp, tool_name, args_json, "
            "result_size_bytes, duration_ms, success) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (old, "sysmon", "{}", 10, 1.0, 1),
                (old, "calendar", "{}", 10, 1.0, 1),
                (recent, "sysmon", "{}", 10, 1.0, 1),
            ],
        )
        conn.commit()
        conn.close()

        deleted = purge.purge_tool_audit()
        assert deleted == 2

        check = sqlite3.connect(str(purge.TOOL_AUDIT_DB))
        remaining = check.execute("SELECT timestamp FROM tool_calls").fetchall()
        check.close()
        assert [r[0] for r in remaining] == [recent]

    def test_missing_database_is_not_an_error(self, tmp_workspace, monkeypatch):
        purge, _ = self._modules(tmp_workspace, monkeypatch)
        assert purge.purge_tool_audit() == 0
