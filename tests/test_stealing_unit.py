"""Work stealing (step 2.4): pure helpers, registry key, claim_stolen predicates."""
import asyncio
import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite")
pytest.importorskip("aiohttp")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import msgqueue  # noqa: E402
import registry  # noqa: E402
import stealing  # noqa: E402
from shards import ShardSpec  # noqa: E402

THIEF, VICTIM = "a-2", "a"
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _load(workspace):
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("ags_steal_unit", ROOT / "bin" / "agent-server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ags_steal_unit"] = mod
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


def ts(age_s):
    """created_at string for a row `age_s` old at NOW (one-second resolution)."""
    return (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")


async def add(db, name, age=60, agent=VICTIM, channel="c", channel_id="5", processed=0,
              call_id=None, reply_to=None, priority=0, not_before=None, expires=None):
    await db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id,"
        " priority, call_id, reply_to_agent, not_before, expires_at, processed, created_at)"
        " VALUES (?, ?, ?, 'u', 'x', ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent, channel, channel_id, name, priority, call_id, reply_to, not_before,
         expires, processed, ts(age)))
    await db.commit()


async def steal(db, limit=5, min_age=1.0):
    rows = await msgqueue.claim_stolen(db, THIEF, VICTIM, limit, min_age, NOW)
    return [r["message_id"] for r in rows]


def with_db(ags, body):
    async def go():
        await ags.init_db()
        try:
            await body(ags.db)
        finally:
            await ags.db.close()
    run(go())


# -- config -------------------------------------------------------------------

def test_steal_config_defaults_and_values():
    assert stealing.steal_config({}) == stealing.StealConfig(False, 5, 5)
    assert stealing.steal_config({"work_stealing": None}).enabled is False
    c = stealing.steal_config({"work_stealing": {"enabled": True, "after_s": 2, "max_rows": 3}})
    assert (c.enabled, c.after_s, c.max_rows) == (True, 2, 3)
    assert stealing.steal_config({"work_stealing": {"enabled": True}}).after_s == 5


def _parse(extra):
    body = {"name": "A", "role": "primary", **extra}
    data = {"version": registry.REGISTRY_VERSION,
            "agents": {"a": body, "m": {"name": "M", "role": "monitor"}}}
    return registry.parse_registry(data)


def test_registry_validation_bounds():
    reg = _parse({"work_stealing": {"enabled": True, "after_s": 0, "max_rows": 20}})
    assert reg.legacy_view()["agents"]["a"]["work_stealing"] == {
        "enabled": True, "after_s": 0, "max_rows": 20}
    assert "work_stealing" not in _parse({}).legacy_view()["agents"]["a"]
    for bad in ({"after_s": 301}, {"after_s": -1}, {"max_rows": 0}, {"max_rows": 21},
                {"max_rows": 1.5}, {"enabled": "yes"}, {"after_s": True}):
        with pytest.raises(registry.RegistryError):
            _parse({"work_stealing": bad})
    with pytest.raises(registry.RegistryError):
        _parse({"work_stealing": [1]})
    reg = _parse({"work_stealing": {"enabled": True, "bogus": 1}})
    assert any("work_stealing.bogus" in w for w in reg.warnings)


def test_min_age_s():
    sc = stealing.StealConfig(True, 5, 5)
    assert stealing.min_age_s(sc, {}) == 5
    assert stealing.min_age_s(sc, {"steering": {"coalesce_ms": 300}}) == 5
    assert stealing.min_age_s(sc, {"steering": {"coalesce_ms": 8000}}) == 8
    assert stealing.min_age_s(sc, {"steering": {"enabled": False, "coalesce_ms": 8000}}) == 5
    assert stealing.min_age_s(stealing.StealConfig(True, 0, 5),
                              {"steering": {"coalesce_ms": 800}}) == 0.8
    assert stealing.min_age_s(stealing.StealConfig(True, 0, 5),
                              {"steering": {"enabled": False, "coalesce_ms": 800}}) == 0


SPECS = [ShardSpec("a", "a"), ShardSpec("a-2", "a", (), False), ShardSpec("a-3", "a", (), False),
         ShardSpec("b", "b")]


def test_candidate_victims():
    st = {"a": "PROCESSING", "a-2": "IDLE", "a-3": "ERROR_RECOVERY", "b": "PROCESSING"}
    assert stealing.candidate_victims(SPECS, "a-2", st, {"a": 1, "a-3": 1, "b": 9}) == ["a", "a-3"]
    # deeper queue first
    assert stealing.candidate_victims(SPECS, "a-2", st, {"a": 1, "a-3": 4}) == ["a-3", "a"]
    # IDLE never a victim; the thief itself never; empty queue never; other agent never
    st2 = {"a": "IDLE", "a-2": "PROCESSING", "a-3": "PROCESSING", "b": "PROCESSING"}
    assert stealing.candidate_victims(SPECS, "a-2", st2, {"a": 3, "a-2": 3, "a-3": 0, "b": 5}) == []
    assert stealing.candidate_victims(SPECS, "b", st, {"a": 3}) == []


def test_thief_ready():
    assert stealing.thief_ready("IDLE", None, 0)
    assert not stealing.thief_ready("PROCESSING", None, 0)
    assert not stealing.thief_ready("ERROR_RECOVERY", None, 0)
    assert not stealing.thief_ready("IDLE", 1234, 0)
    assert not stealing.thief_ready("IDLE", None, 2)


# -- claim_stolen -------------------------------------------------------------

def test_ordinary_old_row_claimed_with_keys(ags):
    async def body(db):
        await add(db, "x1")
        assert await steal(db) == ["x1"]
        r = (await db.execute_fetchall("SELECT * FROM message_queue"))[0]
        assert (r["agent"], r["claimed_by"], r["processed"]) == (VICTIM, THIEF, 1)
        assert r["processing_started_at"]
    with_db(ags, body)


def test_stolen_batch_holds_one_channel(ags):
    async def body(db):
        await add(db, "a1", age=60, channel_id="5")
        await add(db, "b1", age=50, channel_id="6")
        await add(db, "a2", age=40, channel_id="5")
        assert await steal(db) == ["a1", "a2"]
        assert await steal(db) == ["b1"]
    with_db(ags, body)


@pytest.mark.parametrize("name,kw", [
    ("call", {"call_id": "c1", "reply_to": "b"}),
    ("reply", {"call_id": "c1"}),
    ("buzz", {"channel": "hive"}),
    ("callchan", {"channel": "call"}),
    ("priority", {"priority": 5}),
    ("held", {"not_before": int(NOW.timestamp()) + 600}),
    ("expired", {"expires": "2026-10-03T11:00:00Z"}),
    ("young", {"age": 0}),
])
def test_ineligible_rows_left_queued(ags, name, kw):
    async def body(db):
        await add(db, name, **kw)
        assert await steal(db) == []
        r = (await db.execute_fetchall("SELECT processed, claimed_by FROM message_queue"))[0]
        assert r["claimed_by"] is None and r["processed"] in (0, 4)
        if name != "expired":
            assert r["processed"] == 0
    with_db(ags, body)


def test_age_floor_never_younger_than_min_age(ags):
    async def body(db):
        # created_at has second resolution: a row stamped 1s ago may be 0.0-1.0s
        # old, so with min_age 0.3 it is not taken; one stamped 2s ago (>= 1.0s old) is.
        await add(db, "y1", age=1)
        assert await steal(db, min_age=0.3) == []
        await add(db, "y2", age=2, channel_id="6")
        assert await steal(db, min_age=0.3) == ["y2"]
        # a longer floor: 800ms on a row stamped 1s ago is still too young
        await add(db, "y3", age=1, channel_id="7")
        assert await steal(db, min_age=0.8) == []
        await add(db, "y4", age=10, channel_id="8")
        assert await steal(db, min_age=5) == ["y4"]
        await add(db, "y5", age=5, channel_id="9")
        assert await steal(db, min_age=5) == []
    with_db(ags, body)


def test_continuity_in_progress_channel(ags):
    async def body(db):
        await add(db, "busy", processed=1, channel_id="5")
        await add(db, "w1", channel_id="5")
        await add(db, "w2", channel_id="6")
        assert await steal(db) == ["w2"]
    with_db(ags, body)


def test_continuity_earlier_ineligible_row_blocks_later(ags):
    async def body(db):
        await add(db, "early-prio", age=90, channel_id="5", priority=3)
        await add(db, "later", age=60, channel_id="5")
        await add(db, "other", age=50, channel_id="6")
        assert await steal(db) == ["other"]
    with_db(ags, body)


def test_continuity_young_earlier_row_blocks_later(ags):
    async def body(db):
        # an earlier row too young blocks; (a later row is never older than it)
        await add(db, "e", age=3, channel_id="5")
        await add(db, "l", age=3, channel_id="5")
        assert await steal(db, min_age=5) == []
    with_db(ags, body)


def test_channel_zero_exempt(ags):
    async def body(db):
        await add(db, "busy0", processed=1, channel_id="0")
        await add(db, "p0", age=90, channel_id="0", priority=2)
        await add(db, "z1", age=60, channel_id="0")
        assert await steal(db) == ["z1"]
    with_db(ags, body)


def test_limit_and_order(ags):
    async def body(db):
        for i, age in enumerate((10, 50, 30, 40, 20)):
            await add(db, f"o{age}", age=age, channel_id="0")
        assert await steal(db, limit=3) == ["o50", "o40", "o30"]
        assert await steal(db, limit=3) == ["o20", "o10"]
    with_db(ags, body)


def test_other_shards_untouched(ags):
    async def body(db):
        await add(db, "mine", agent=THIEF)
        await add(db, "b1", agent="b")
        assert await steal(db) == []
    with_db(ags, body)


def test_expire_runs_first(ags):
    async def body(db):
        await add(db, "e1", expires="2026-10-03T11:59:00Z")
        assert await steal(db) == []
        r = (await db.execute_fetchall("SELECT processed, response FROM message_queue"))[0]
        assert (r["processed"], r["response"]) == (4, "expired")
    with_db(ags, body)


def test_steal_wait_s_and_depths(ags):
    async def body(db):
        assert await msgqueue.steal_wait_s(db, VICTIM, 1, NOW) is None
        await add(db, "call", call_id="c", reply_to="b")
        assert await msgqueue.steal_wait_s(db, VICTIM, 1, NOW) is None
        await add(db, "young", age=0)
        w = await msgqueue.steal_wait_s(db, VICTIM, 1, NOW)
        assert 1.5 <= w <= 2.0  # stamped now: eligible once c+1+min_age has passed
        await add(db, "old", age=60, channel_id="6")
        assert await msgqueue.steal_wait_s(db, VICTIM, 1, NOW) < 0
        await add(db, "rep", call_id="c2")  # reply row: not counted
        d = await msgqueue.queued_depths(db, [VICTIM, THIEF])
        assert d == {VICTIM: 3, THIEF: 0}
    with_db(ags, body)


def test_double_claim_partitions_200_iterations(ags):
    async def body(db):
        await db.execute("PRAGMA synchronous = OFF")
        for it in range(200):
            for i in range(6):
                await add(db, f"m{it}-{i}", channel_id="0")
            r1, r2 = await asyncio.gather(
                msgqueue.claim_batch(db, VICTIM, 4),
                msgqueue.claim_stolen(db, THIEF, VICTIM, 4, 1.0, NOW))
            ids1, ids2 = {r["id"] for r in r1}, {r["id"] for r in r2}
            assert not ids1 & ids2
            assert len(ids1) + len(ids2) == 6
            await db.execute("UPDATE message_queue SET processed = 2")
            await db.commit()
    with_db(ags, body)


# -- no lock held across an await -----------------------------------------------

def test_reads_leave_no_lock_between_hops(ags):
    """A read that awaits between execute() and its cursor being closed keeps
    SQLite's SHARED lock while the loop is free to run other code; a synchronous
    writer on the same file (the harness tests) then waits out its busy timeout.
    Probe the file from a second connection on every loop tick while the queue
    helpers and agent_hold_until run."""
    import sqlite3

    async def body(db):
        await add(db, "w1", age=60, agent=VICTIM)
        await add(db, "w2", age=60, agent=VICTIM, not_before=int(time.time()) + 600)
        await db.commit()
        probe = sqlite3.connect(str(ags.DB_PATH), timeout=0, isolation_level=None)
        locked = []
        real_execute = db._execute

        async def probed(fn, *a, **kw):
            # Right after a hop returns, the caller is back on the loop and the
            # connection thread is idle: nothing may still hold the file.
            result = await real_execute(fn, *a, **kw)
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.execute("ROLLBACK")
            except sqlite3.OperationalError as e:
                locked.append(str(e))
            return result
        db._execute = probed

        await msgqueue.peek_claimable(db, VICTIM)
        await msgqueue.steal_wait_s(db, VICTIM, 0.3)
        await msgqueue.queued_depths(db, [VICTIM, THIEF])
        await ags.agent_hold_until(VICTIM)
        await msgqueue.fail_calls(db, VICTIM, "x")
        await msgqueue.claim_batch(db, VICTIM, 5)
        db._execute = real_execute
        probe.close()
        assert not locked, f"lock held across an await {len(locked)}x"

    with_db(ags, body)
