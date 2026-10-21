"""lib/msgqueue.py (spec 1.2): ordering, expiry, call rows, double claim, wake-up."""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite")
pytest.importorskip("aiohttp")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import msgqueue  # noqa: E402


def _load(workspace):
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("ags_queue_module", ROOT / "bin" / "agent-server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ags_queue_module"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return mod


@pytest.fixture
def ags(tmp_path):
    return _load(tmp_path)


def run(coro):
    return asyncio.run(coro)


async def add(db, shard, n, priority=0, call_id=None, reply_to=None, expires=None, created=None):
    await db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id,"
        " priority, call_id, reply_to_agent, expires_at, created_at)"
        " VALUES (?, 'c', '0', 'u', 'x', ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (shard, f"m{shard}{n}", priority, call_id, reply_to, expires, created))
    await db.commit()


def test_priority_then_age_then_id(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        await add(db, "a", 1, created="2026-01-01 00:00:01")
        await add(db, "a", 2, priority=5, created="2026-01-01 00:00:09")
        await add(db, "a", 3, created="2026-01-01 00:00:01")
        await add(db, "a", 4, priority=5, created="2026-01-01 00:00:02")
        rows = await msgqueue.claim_batch(db, "a", 20, "2026-06-01T00:00:00Z")
        assert [r["message_id"] for r in rows] == ["ma4", "ma2", "ma1", "ma3"]
        assert all(r["claimed_by"] == "a" and r["processed"] == 1 for r in rows)
        assert await msgqueue.claim_batch(db, "a", 20) == []
        await db.close()
    run(go())


def test_limit_and_other_shard_untouched(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        for i in range(25):
            await add(db, "a", i)
        await add(db, "b", 0)
        assert len(await msgqueue.claim_batch(db, "a", 20)) == 20
        assert len(await msgqueue.claim_batch(db, "a", 20)) == 5
        assert len(await msgqueue.claim_batch(db, "b", 20)) == 1
        await db.close()
    run(go())


def test_expiry_skips_and_ordinary_row_has_no_reply(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        await add(db, "a", 1, expires="2026-01-01T00:00:00Z")
        await add(db, "a", 2, expires="2999-01-01T00:00:00Z")
        rows = await msgqueue.claim_batch(db, "a", 20, "2026-06-01T00:00:00Z")
        assert [r["message_id"] for r in rows] == ["ma2"]
        async with db.execute("SELECT processed, response FROM message_queue WHERE message_id='ma1'") as c:
            r = await c.fetchone()
        assert (r["processed"], r["response"]) == (msgqueue.STATUS_SKIPPED, "expired")
        async with db.execute("SELECT COUNT(*) n FROM message_queue") as c:
            assert (await c.fetchone())["n"] == 2          # no reply row
        await db.close()
    run(go())


def test_call_row_claimed_alone_and_expired_call_replies(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        await add(db, "a", 1, priority=9, call_id="c1", reply_to="caller")
        await add(db, "a", 2)
        await add(db, "a", 3)
        first = await msgqueue.claim_batch(db, "a", 20)
        assert [r["call_id"] for r in first] == ["c1"]
        second = await msgqueue.claim_batch(db, "a", 20)
        assert [r["message_id"] for r in second] == ["ma2", "ma3"]
        # a call row behind ordinary rows is not swept into their batch
        await add(db, "a", 4)
        await add(db, "a", 5, call_id="c2", reply_to="caller")
        batch = await msgqueue.claim_batch(db, "a", 20)
        assert [r["message_id"] for r in batch] == ["ma4"]
        await db.close()
    run(go())

    async def go2():
        a2 = _load(ags.WORKSPACE_ROOT / "second")
        await a2.init_db()
        db = a2.db
        await add(db, "a", 1, call_id="c9", reply_to="caller", expires="2026-01-01T00:00:00Z")
        assert await msgqueue.expire(db, "a", "2026-06-01T00:00:00Z") == 1
        async with db.execute("SELECT * FROM message_queue WHERE agent='caller'") as c:
            reply = await c.fetchone()
        assert reply["call_id"] == "c9" and reply["processed"] == 0
        assert json.loads(reply["content"]) == {"error": "expired", "call_id": "c9"}
        # idempotent: a second expire pass writes nothing more
        assert await msgqueue.expire(db, "a", "2026-06-01T00:00:00Z") == 0
        async with db.execute("SELECT COUNT(*) n FROM message_queue") as c:
            assert (await c.fetchone())["n"] == 2
        await db.close()
    run(go2())


def test_reply_row_is_never_claimed_and_is_reaped(ags):
    """A reply row (call_id set, reply_to_agent NULL) is read by the waiting
    caller, never run as a turn (step 2.3); unconsumed ones are skipped."""
    async def go():
        await ags.init_db()
        db = ags.db
        await add(db, "a", 1, call_id="c1", reply_to=None, created="2026-01-01 00:00:00")
        await add(db, "a", 2)
        batch = await msgqueue.claim_batch(db, "a", 20)
        assert [r["message_id"] for r in batch] == ["ma2"]  # ordinary row only
        assert await msgqueue.claim_batch(db, "a", 20) == []
        # fresh reply rows survive the reaper; old ones are skipped
        assert await msgqueue.reap_hive_rows(db, 1767225600 + 60, 600) == 0
        assert await msgqueue.reap_hive_rows(db, 1767225600 + 700, 600) == 1
        async with db.execute("SELECT processed, response FROM message_queue"
                              " WHERE message_id='ma1'") as c:
            row = await c.fetchone()
        assert (row["processed"], row["response"]) == (msgqueue.STATUS_SKIPPED, "stale")
        await db.close()
    run(go())


def test_release_and_partial(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        await add(db, "a", 1)
        [row] = await msgqueue.claim_batch(db, "a", 5)
        await msgqueue.set_partial(db, row["id"], "half")
        assert await msgqueue.release(db, [row["id"]]) == 1
        again = await msgqueue.claim_batch(db, "a", 5)
        assert again[0]["partial_response"] == "half" and again[0]["id"] == row["id"]
        await db.close()
    run(go())


def test_double_claim_partitions_200_iterations(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        await db.execute("PRAGMA synchronous = OFF")
        for it in range(200):
            for i in range(6):
                await add(db, "a", f"{it}-{i}")
            r1, r2 = await asyncio.gather(
                msgqueue.claim_batch(db, "a", 4), msgqueue.claim_batch(db, "a", 4))
            ids1, ids2 = {r["id"] for r in r1}, {r["id"] for r in r2}
            assert not ids1 & ids2
            assert len(ids1) + len(ids2) == 6
        await db.close()
    run(go())


def test_wait_for_work():
    async def go():
        async def poke(shard, delay):
            await asyncio.sleep(delay)
            msgqueue.notify(shard)
        t = asyncio.create_task(poke("a", 0.05))
        assert await msgqueue.wait_for_work("a", 2) is True
        await t
        assert not await msgqueue.wait_for_work("a", 0.1)
        # a row for shard B does not wake a waiter on shard A
        t = asyncio.create_task(poke("b", 0.05))
        assert not await msgqueue.wait_for_work("a", 0.3)
        await t
    run(go())


def test_boot_refuses_unmigrated_queue(ags):
    import sqlite3
    ags.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ags.DB_PATH)
    con.execute("CREATE TABLE message_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,"
                " channel TEXT NOT NULL, channel_id TEXT NOT NULL, author TEXT NOT NULL,"
                " content TEXT NOT NULL, message_id TEXT UNIQUE NOT NULL, processed INTEGER DEFAULT 0,"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    con.commit()
    con.close()
    with pytest.raises(SystemExit) as e:
        run(ags.init_db())
    assert "karakos migrate" in str(e.value)


# -- 2.7: peek_claimable and fail_calls -------------------------------------------

def test_peek_claimable_is_exactly_what_claim_batch_claims(ags, tmp_path):
    async def go():
        await ags.init_db()
        db = ags.db
        try:
            await add(db, "s", 1, priority=0)
            await add(db, "s", 2, priority=5)
            await add(db, "s", 3, expires="2000-01-01T00:00:00Z")      # expired
            await add(db, "s", 4, call_id="c", reply_to="o")           # call row, not head
            await add(db, "s", 5, priority=0)
            await add(db, "s", 7, priority=99, call_id="r")             # reply row: never claimed
            peeked = await msgqueue.peek_claimable(db, "s", 2)
            before = await db.execute_fetchall("SELECT id, processed FROM message_queue")
            claimed = await msgqueue.claim_batch(db, "s", 2)
            assert [r["id"] for r in peeked] == [r["id"] for r in claimed]
            assert all(r["processed"] == 0 for r in before)           # peek claimed nothing
            # a call row at the head is claimed alone
            await add(db, "s", 6, priority=9, call_id="c2", reply_to="o")
            peeked = await msgqueue.peek_claimable(db, "s", 20)
            claimed = await msgqueue.claim_batch(db, "s", 20)
            assert [r["id"] for r in peeked] == [r["id"] for r in claimed] and len(claimed) == 1
            assert await msgqueue.peek_claimable(db, "empty", 20) == []
        finally:
            await db.close()
    run(go())


async def add_chan(db, shard, n, channel_id, is_bot=0, priority=0, created=None, channel="c"):
    await db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, author, is_bot, content,"
        " message_id, priority, created_at)"
        " VALUES (?, ?, ?, 'u', ?, 'x', ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (shard, channel, channel_id, is_bot, f"m{shard}{n}", priority, created))
    await db.commit()


def test_batch_never_mixes_channels(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        try:
            await add_chan(db, "s", 1, "A", created="2026-01-01 00:00:01")
            await add_chan(db, "s", 2, "B", created="2026-01-01 00:00:02")
            await add_chan(db, "s", 3, "A", created="2026-01-01 00:00:03")
            peeked = await msgqueue.peek_claimable(db, "s", 20)
            first = await msgqueue.claim_batch(db, "s", 20)
            assert [r["message_id"] for r in first] == ["ms1", "ms3"]
            assert {r["channel_id"] for r in first} == {"A"}
            assert [r["id"] for r in peeked] == [r["id"] for r in first]
            peeked = await msgqueue.peek_claimable(db, "s", 20)
            second = await msgqueue.claim_batch(db, "s", 20)
            assert [r["message_id"] for r in second] == ["ms2"]
            assert {r["channel_id"] for r in second} == {"B"}
            assert [r["id"] for r in peeked] == [r["id"] for r in second]
            assert await msgqueue.claim_batch(db, "s", 20) == []
        finally:
            await db.close()
    run(go())


def test_human_row_in_other_channel_claimed_before_older_heartbeat(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        try:
            await add_chan(db, "s", 1, "A", is_bot=1, created="2026-01-01 00:00:01",
                           channel="signals")
            await add_chan(db, "s", 2, "B", is_bot=0, created="2026-01-01 00:00:05",
                           channel="lounge")
            peeked = await msgqueue.peek_claimable(db, "s", 20)
            first = await msgqueue.claim_batch(db, "s", 20)
            assert [r["message_id"] for r in first] == ["ms2"]
            assert first[0]["channel_id"] == "B"
            assert [r["id"] for r in peeked] == [r["id"] for r in first]
            second = await msgqueue.claim_batch(db, "s", 20)
            assert [r["message_id"] for r in second] == ["ms1"]
        finally:
            await db.close()
    run(go())


def test_fail_calls_replies_and_skips_only_call_rows(ags):
    async def go():
        await ags.init_db()
        db = ags.db
        try:
            await add(db, "s", 1)
            await add(db, "s", 2, call_id="c1", reply_to="o")
            await add(db, "s", 3, call_id="c9")   # a reply row a waiting caller will read
            assert await msgqueue.fail_calls(db, "s", "callee_paused", "token budget") == 1
            assert await msgqueue.fail_calls(db, "s", "callee_paused", "token budget") == 0
            rows = {r["message_id"]: r for r in await db.execute_fetchall(
                "SELECT * FROM message_queue")}
            assert rows["ms2"]["processed"] == msgqueue.STATUS_SKIPPED
            assert rows["ms2"]["response"] == "callee_paused"
            assert rows["ms1"]["processed"] == 0
            assert rows["ms3"]["processed"] == 0   # replies are never failed
            reply = json.loads(rows["callee_paused-c1-" + str(rows["ms2"]["id"])]["content"])
            assert reply == {"call_id": "c1", "error": "callee_paused", "detail": "token budget"}
        finally:
            await db.close()
    run(go())
