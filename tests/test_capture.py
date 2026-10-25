"""
Tests for bin/capture.py — Message persistence to JSONL.
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT


class TestCaptureMessage:
    """Test the capture_message function."""

    def test_creates_jsonl_file(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        capture = import_script("capture")

        msg = {
            "ts": "2026-04-10T12:00:00Z",
            "channel": "general",
            "author": "testuser",
            "content": "Hello world",
            "message_id": "msg-001",
        }
        capture.capture_message(msg)

        log_file = tmp_workspace / "data" / "messages" / "messages-2026-04-10.jsonl"
        assert log_file.exists(), "JSONL file not created"

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["channel_name"] == "general"
        assert entry["author_name"] == "testuser"
        assert entry["content"] == "Hello world"
        assert entry["message_id"] == "msg-001"

    def test_appends_multiple_messages(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        capture = import_script("capture")

        for i in range(5):
            capture.capture_message({
                "ts": "2026-04-10T12:00:00Z",
                "channel": "general",
                "author": f"user{i}",
                "content": f"Message {i}",
                "message_id": f"msg-{i:03d}",
            })

        log_file = tmp_workspace / "data" / "messages" / "messages-2026-04-10.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 5

    def test_generates_timestamp_if_missing(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        capture = import_script("capture")

        capture.capture_message({
            "channel": "general",
            "author": "testuser",
            "content": "No timestamp",
            "message_id": "msg-no-ts",
        })

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = tmp_workspace / "data" / "messages" / f"messages-{today}.jsonl"
        assert log_file.exists()

        entry = json.loads(log_file.read_text().strip())
        assert entry["ts"]

    def test_separates_by_date(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        capture = import_script("capture")

        capture.capture_message({
            "ts": "2026-04-09T23:59:00Z",
            "channel": "general",
            "author": "user",
            "content": "Yesterday",
            "message_id": "msg-yesterday",
        })
        capture.capture_message({
            "ts": "2026-04-10T00:01:00Z",
            "channel": "general",
            "author": "user",
            "content": "Today",
            "message_id": "msg-today",
        })

        assert (tmp_workspace / "data" / "messages" / "messages-2026-04-09.jsonl").exists()
        assert (tmp_workspace / "data" / "messages" / "messages-2026-04-10.jsonl").exists()

    def test_handles_missing_fields_gracefully(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        capture = import_script("capture")

        capture.capture_message({"ts": "2026-04-10T12:00:00Z"})

        log_file = tmp_workspace / "data" / "messages" / "messages-2026-04-10.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["channel_name"] == ""
        assert entry["author_name"] == ""
        assert entry["content"] == ""
        assert entry["is_bot"] is False


class TestBackfill:
    """Test --backfill against the database the agent server writes.

    Nothing exercised ``backfill`` before, so its ``db_path`` pointed at
    ``data/agent-server.db`` — the queue actually lives at
    ``data/memory/agent-server.db`` — and every run exited 1 with "Database
    not found" (#150). That early exit also hid a second defect one line
    further in: ``row.get(...)`` on a ``sqlite3.Row``, which has no ``.get``.

    The guard is deliberately *not* another string literal: asserting
    ``capture.QUEUE_DB == agent_server.DB_PATH`` fails whenever reader and
    writer drift apart, which a duplicated literal cannot do.
    """

    def _modules(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        capture = import_script("capture")
        agent_server = import_script("agent-server")
        return capture, agent_server

    def test_backfill_path_matches_the_writer(self, tmp_workspace, monkeypatch):
        capture, agent_server = self._modules(tmp_workspace, monkeypatch)
        assert capture.QUEUE_DB == agent_server.DB_PATH

    def test_backfill_path_is_one_something_actually_writes(self, tmp_workspace, monkeypatch):
        """Let the real writer create its database, then look for it.

        A backfill aimed at a path nothing writes fails here even if the two
        constants above were changed in lockstep to a third wrong value.
        """
        capture, agent_server = self._modules(tmp_workspace, monkeypatch)

        asyncio.run(_init_and_close(agent_server))

        assert capture.QUEUE_DB.exists(), (
            f"backfill reads {capture.QUEUE_DB}, which the agent server "
            f"did not create"
        )

    def test_backfill_exports_queued_messages(self, tmp_workspace, monkeypatch, capsys):
        capture, agent_server = self._modules(tmp_workspace, monkeypatch)

        # Schema comes from the writer, so the columns backfill reads are the
        # ones agent-server actually creates.
        asyncio.run(_init_and_close(agent_server))

        conn = sqlite3.connect(str(agent_server.DB_PATH))
        conn.executemany(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author, "
            "author_id, is_bot, content, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("amos", "general", "123", "discord", "mike", "42", 0,
                 "hello", "m-1", "2026-04-10 09:00:00"),
                ("amos", "general", "123", "discord", "amos", "7", 1,
                 "hi back", "m-2", "2026-04-10 09:00:05"),
                ("amos", "general", "123", "discord", "mike", "42", 0,
                 "next day", "m-3", "2026-04-11 09:00:00"),
            ],
        )
        conn.commit()
        conn.close()

        capture.backfill("2026-04-10")

        log_file = tmp_workspace / "data" / "messages" / "messages-2026-04-10.jsonl"
        entries = [json.loads(line) for line in log_file.read_text().strip().split("\n")]

        assert [e["message_id"] for e in entries] == ["m-1", "m-2"]
        assert entries[0]["content"] == "hello"
        assert entries[0]["is_bot"] is False
        assert entries[1]["is_bot"] is True
        assert entries[0]["server"] == "discord"
        assert entries[0]["author_name"] == "mike"
        assert entries[0]["channel_name"] == "general"

    def test_backfill_exports_the_plain_name_not_the_rename_note(self, tmp_workspace, monkeypatch):
        capture, agent_server = self._modules(tmp_workspace, monkeypatch)
        asyncio.run(_init_and_close(agent_server))
        conn = sqlite3.connect(str(agent_server.DB_PATH))
        conn.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author, "
            "author_id, is_bot, content, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("amos", "general", "123", "discord", "Alice\x1falice_old", "42", 0,
             "hi", "m-1", "2026-04-10 09:00:00"))
        conn.commit()
        conn.close()
        capture.backfill("2026-04-10")
        log_file = tmp_workspace / "data" / "messages" / "messages-2026-04-10.jsonl"
        assert json.loads(log_file.read_text().strip())["author_name"] == "Alice"

    def test_backfill_exits_when_database_is_absent(self, tmp_workspace, monkeypatch):
        capture, _ = self._modules(tmp_workspace, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            capture.backfill("2026-04-10")
        assert exc.value.code == 1


async def _init_and_close(agent_server):
    """Create the queue schema exactly as the running server would."""
    await agent_server.init_db()
    await agent_server.db.close()
    agent_server.db = None

