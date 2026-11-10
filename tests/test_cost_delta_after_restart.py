"""A server restart must not book a resumed session's whole total as one cost delta."""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import aiosqlite
import pytest

AGENT_SERVER = Path(__file__).parent.parent / "bin" / "agent-server.py"


@pytest.fixture
def ags(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("ags_cost_under_test", AGENT_SERVER)
        m = importlib.util.module_from_spec(spec)
        sys.modules["ags_cost_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    m.channels_config = {}
    return m


async def _run(ags, tmp_path, turns):
    ags.db = await aiosqlite.connect(str(tmp_path / "cost.db"))
    ags.db.row_factory = aiosqlite.Row
    await ags.db.execute(
        "CREATE TABLE cost_events (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, cost_delta REAL, "
        "session_total REAL, input_tokens INTEGER, output_tokens INTEGER, duration_ms REAL, "
        "session_id TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await ags.db.commit()
    for restart, total, sid in turns:
        if restart:
            ags.agent_last_cost.clear()
        await ags.post_cost_update("monitor", {"total_cost_usd": total, "session_id": sid})
    async with ags.db.execute("SELECT cost_delta FROM cost_events ORDER BY id") as cur:
        deltas = [r["cost_delta"] for r in await cur.fetchall()]
    await ags.db.close()
    return deltas


def test_restart_does_not_rebook_the_resumed_session_total(ags, tmp_path):
    ags.agent_last_cost.clear()
    deltas = asyncio.run(_run(ags, tmp_path, [
        (False, 19.57, "s1"), (False, 19.62, "s1"), (True, 19.67, "s1"),
    ]))
    assert [round(d, 2) for d in deltas] == [19.57, 0.05, 0.05]


def test_first_turn_of_a_new_session_after_restart_books_its_total(ags, tmp_path):
    ags.agent_last_cost.clear()
    deltas = asyncio.run(_run(ags, tmp_path, [(False, 2.0, "s1"), (True, 0.5, "s2")]))
    assert [round(d, 2) for d in deltas] == [2.0, 0.5]


def test_total_that_drops_within_a_session_books_the_new_total(ags, tmp_path):
    ags.agent_last_cost.clear()
    deltas = asyncio.run(_run(ags, tmp_path, [(False, 3.0, "s1"), (False, 1.0, "s1")]))
    assert [round(d, 2) for d in deltas] == [3.0, 1.0]
