"""Rows queued before a restart are drained at startup, one drain per shard.

Without this, a row queued before an agent-server restart waits until a new
row arrives for that agent: notify_enqueued is the only routine drain trigger.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("aiohttp")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ags(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location("ags_startup_drain", ROOT / "bin" / "agent-server.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["ags_startup_drain"] = m
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("shards", [["amos"], ["amos", "helper"]])
def test_startup_drain_invokes_drain_for_each_shard_with_queued_row(ags, monkeypatch, shards):
    seen = []

    async def recorder(state, shard):
        # Runs under the drain task, while the db is still open: the row
        # queued before the restart must still be there for it to claim.
        async with ags.db.execute(
                "SELECT COUNT(*) FROM message_queue WHERE agent=? AND processed=0",
                (shard,)) as cur:
            seen.append((shard, (await cur.fetchone())[0]))
    monkeypatch.setattr(ags.turn_loop, "drain_shard", recorder)
    for sid in shards:
        ags.agent_config[sid] = {}

    async def go():
        await ags.init_db()
        for sid in shards:
            await ags.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content,"
                " message_id, processed) VALUES (?,'c','55','u','queued before restart',?,0)",
                (sid, f"m-{sid}"))
        await ags.db.commit()
        await ags.drain_all_shards_at_startup()
        await asyncio.gather(*list(ags.STATE.bg_tasks))
        await ags.db.close()
    asyncio.run(go())

    assert sorted(seen) == sorted((sid, 1) for sid in shards)
