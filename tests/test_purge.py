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

    def test_handles_empty_directory(self, tmp_workspace, monkeypatch):
        purge = self._make_purger(tmp_workspace, monkeypatch)
        deleted = purge.purge_old_session_summaries()
        assert deleted == 0
