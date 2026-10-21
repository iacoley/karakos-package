"""The 2.7 gate end to end: real agent-server, fake `claude`.

Agent `a` has shards `a` and `a-2`; `b` has its default shard; the harness adds a
`monitor`. Time is `usage_gate._now`; resume timers poll it every 20 ms.
"""

import asyncio
import json
import sqlite3
import sys
import time

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import usage_gate  # noqa: E402

SHARDS = {"a": ["a", "a-2"]}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock(monkeypatch):
    t = [time.time()]
    monkeypatch.setattr(usage_gate, "_now", lambda: t[0])
    monkeypatch.setattr(usage_gate, "RESUME_POLL_S", 0.02)
    return t


async def sql(h, query, params=()):
    """Write through the server's own connection: a second writer on the file
    can hit 'database is locked' while the server finishes a turn."""
    await h.module.db.execute(query, params)
    await h.module.db.commit()


def queued(h, shard):
    return [r for r in h.queue_rows(shard) if r["processed"] == 0]


async def usage(h):
    resp = await h.client.get("/usage", headers=h._headers())
    return await resp.json()


async def settle(h, secs=0.3):
    await asyncio.sleep(secs)


async def set_week(h, pct, reset_in=86400):
    await sql(h, "INSERT INTO rate_limit_state (rate_limit_type, status, resets_at, utilization)"
           " VALUES ('seven_day', 'allowed', ?, ?)"
           " ON CONFLICT(rate_limit_type) DO UPDATE SET utilization = excluded.utilization,"
           " resets_at = excluded.resets_at", (int(time.time()) + reset_in, pct))


# -- breaker ----------------------------------------------------------------

def test_breaker_stops_every_shard_and_wakes_itself(harness, clock):
    h = harness(agents=["a", "b"], shards=SHARDS)
    until_reset = int(clock[0]) + 3600

    async def scenario():
        async with h:
            h.script(rules=[{"match": "TRIGGER", "step": {"rate_limit": {
                "status": "rejected", "type": "seven_day", "resets_at": until_reset}}}])
            await h.send("a", "TRIGGER")
            await h.wait_idle("a")
            before = {s: h.sent_to(s) for s in ("a-2", "b")}
            await h.send("a-2", "after-a2")
            await h.send("b", "after-b")
            await settle(h)
            assert len(queued(h, "a-2")) == 1 and len(queued(h, "b")) == 1
            assert {s: h.sent_to(s) for s in ("a-2", "b")} == before
            body = await usage(h)
            assert body["breaker"]["paused"] is True
            assert body["breaker"]["types"] == ["seven_day"]
            assert "seven_day" in body["windows"]
            agents = (await (await h.client.get("/agents", headers=h._headers())).json())
            a2 = [s for ag in agents["agents"] for s in ag["shards"] if s["id"] == "a-2"][0]
            assert a2["paused"]["reason"] == "breaker"
            # time passes; nobody sends anything
            clock[0] = until_reset + 60
            await h.wait_idle("a-2")
            await h.wait_idle("b")
            assert h.sent_to("a-2") and h.sent_to("b")

    run(scenario())
    # one human notice per shard pause, wall-notice wording
    notes = [d for d in h.discord if "account usage limit" in d["content"]]
    assert len(notes) == 2


# -- budget -----------------------------------------------------------------

def test_budget_pauses_the_agent_with_hysteresis_and_one_notice(harness, clock):
    h = harness(agents={"a": {"token_budget_4h": 1000}, "b": {}}, shards=SHARDS)

    async def scenario():
        async with h:
            h.module.RATE_LIMIT_ALERT_CHANNEL_ID = "999"
            for shard in ("a", "a-2"):
                await sql(h, "INSERT INTO cost_events (agent, cost_delta, session_total,"
                       " input_tokens, output_tokens) VALUES (?, 0, 0, 400, 200)", (shard,))
            await h.send("a", "one")
            await h.send("a-2", "two")
            await settle(h)
            assert len(queued(h, "a")) == 1 and len(queued(h, "a-2")) == 1
            await h.wait_for(lambda: any("token budget" in d["content"]
                                         and d["channel_id"] == "1" for d in h.discord))
            await h.send("a", "one-more")  # same pause: no second human notice
            await settle(h)
            human = [d for d in h.discord if d["channel_id"] == "1"
                     and "token budget" in d["content"] and d["agent"] == "a"]
            assert len(human) == 1
            body = await usage(h)
            assert body["budgets"]["a"]["used"] == 1200
            assert body["budgets"]["a"]["budget"] == 1000
            # usage drops, but the minimum pause has not elapsed
            await sql(h, "DELETE FROM cost_events")
            clock[0] += 29 * 60
            await h.send("a", "three")
            await settle(h)
            assert queued(h, "a")
            # +31 min: under budget and past the minimum; the timer drains both
            clock[0] += 2 * 60
            await h.wait_idle("a")
            await h.wait_idle("a-2")
            assert h.sent_to("a") and h.sent_to("a-2")
            # second crossing inside the notice cooldown pauses without an alert
            for shard in ("a", "a-2"):
                await sql(h, "INSERT INTO cost_events (agent, cost_delta, session_total,"
                       " input_tokens, output_tokens) VALUES (?, 0, 0, 900, 900)", (shard,))
            clock[0] += 61  # age out the 60 s usage cache
            await h.send("a", "four")
            await settle(h)
            assert queued(h, "a")

    run(scenario())
    alerts = [d for d in h.discord if d["channel_id"] == "999" and "paused" in d["content"]]
    assert len(alerts) == 1
    assert any("resumed" in d["content"] for d in h.discord if d["channel_id"] == "999")


def test_budget_pause_answers_queued_calls(harness, clock):
    h = harness(agents={"a": {"token_budget_4h": 1000}, "b": {}}, shards=SHARDS)

    async def scenario():
        async with h:
            await sql(h, "INSERT INTO cost_events (agent, cost_delta, session_total,"
                   " input_tokens, output_tokens) VALUES ('a', 0, 0, 900, 900)")
            await sql(h, "INSERT INTO message_queue (agent, channel, channel_id, server,"
                   " author, is_bot, content, message_id, call_id, reply_to_agent)"
                   " VALUES ('a', 'call', '0', 'local', 'b', 1, 'q', 'call-1', 'c1', 'b')")
            await h.send("a", "hello")
            await h.wait_for(lambda: any(r["response"] == "callee_paused"
                                         for r in h.queue_rows("a")))
            rows = {r["message_id"]: r for r in h.queue_rows("a")}
            assert rows["call-1"]["processed"] == 4
            assert rows["call-1"]["response"] == "callee_paused"
            replies = [r for r in h.queue_rows("b")
                       if r["message_id"].startswith("callee_paused-c1")]
            assert replies
            body = json.loads(replies[0]["content"])
            assert body["error"] == "callee_paused" and body["call_id"] == "c1"

    run(scenario())


# -- governor ---------------------------------------------------------------

def test_governor_defers_machine_rows_only(harness, clock):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            await set_week(h, 85)
            await h.poke("a", "heartbeat", "hb1")
            await settle(h)
            assert [r["content"] for r in queued(h, "a")] == ["hb1"]
            assert h.sent_to("a") == []
            await h.poke("a", "heartbeat", "hb2")
            await h.wait_for(lambda: any(r["response"] == "superseded"
                                         for r in h.queue_rows("a")))
            rows = {r["content"]: r for r in h.queue_rows("a")}
            assert rows["hb1"]["response"] == "superseded" and rows["hb1"]["processed"] == 4
            assert rows["hb2"]["processed"] == 0
            # a human message runs; the queued heartbeat is another channel, so
            # it is not in that turn and stays deferred
            await h.send("a", "hello")
            await h.wait_for(lambda: any(r["content"] == "hello" and r["processed"] == 2
                                         for r in h.queue_rows("a")))
            sent = "\n".join(h.sent_to("a"))
            assert "hello" in sent and "hb2" not in sent
            assert {r["content"]: r["processed"] for r in h.queue_rows("a")}["hb2"] == 0
            # scheduler is never gated
            await set_week(h, 99)
            await h.poke("a", "scheduler", "remind me")
            await h.wait_idle("a")
            assert "remind me" in "\n".join(h.sent_to("a"))
            # the monitor is never gated
            await h.poke("monitor", "heartbeat", "mon-hb")
            await h.wait_idle("monitor")
            assert [r["processed"] for r in h.queue_rows("monitor")] == [2]
            # hysteresis on a default-threshold job: 85 defers, 77 holds, 70 resumes
            await set_week(h, 85)
            await h.poke("a", "nightly", "nt")
            await settle(h)
            assert queued(h, "a")
            await set_week(h, 77)
            clock[0] += 301
            await settle(h)
            assert queued(h, "a")
            await set_week(h, 70)
            clock[0] += 301
            await h.wait_idle("a")
            assert "nt" in "\n".join(h.sent_to("a"))

    run(scenario())
    log = (h.workspace / "logs" / "governor.jsonl").read_text().splitlines()
    assert json.loads(log[0])["decision"] == "defer"


def test_unconfigured_gate_changes_nothing(harness, clock):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            await h.send("a", "x")
            await h.send("b", "y")
            await h.wait_idle("a")
            await h.wait_idle("b")
            body = await usage(h)
            assert body["breaker"]["paused"] is False
            assert body["budgets"] == {}

    run(scenario())
