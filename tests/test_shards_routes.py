"""Shard-aware HTTP shapes, reload, ask, shutdown, schema (step 2.1)."""

import asyncio
import json
import logging
import sqlite3
import sys
from unittest import mock

import pytest
import yaml

from conftest import PACKAGE_ROOT

SHARDS = {"a": ["a", "a-2"]}
STATUS_SKIPPED = 4


def run(coro):
    return asyncio.run(coro)


def fixture(harness):
    return harness(agents=["a", "b"], shards=SHARDS)


def edit(h, fn):
    path = h.workspace / "config" / "agents.yaml"
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


async def get(h, path):
    r = await h.client.get(path, headers=h._headers())
    return await r.json()


# -- reporting -------------------------------------------------------------------

def test_agents_and_health_shapes(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                {"match": "slow", "agent": "^a-2$", "step": {"text": "x", "delay_ms": 1500}},
                {"match": "big", "agent": "^a$", "step": {"text": "x", "usage": {
                    "input_tokens": 5, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 9000, "output_tokens": 1}}}])
            await h.send("a", "big")
            await h.wait_idle("a")
            await h.send("a-2", "slow")
            await h.wait_for(lambda: h.module.agent_states.get("a-2") == "PROCESSING")
            h.module.agent_last_channel["a-2"] = "55"
            ag = await get(h, "/agents")
            hl = await get(h, "/health")
            await h.wait_idle("a-2")
            return ag, hl

    ag, hl = run(scenario())
    agents = {x["name"]: x for x in ag["agents"]}
    assert set(agents) == {"a", "b", "monitor"}
    a = agents["a"]
    assert a["state"] == "PROCESSING"
    assert [s["id"] for s in a["shards"]] == ["a", "a-2"]
    assert a["shards"][0]["is_default"] is True and a["shards"][1]["is_default"] is False
    assert set(a["shards"][1]) == {"id", "is_default", "state", "alive", "pid", "session_id",
                                   "queue_depth", "context_tokens", "channels", "last_channel", "paused",
                                       "stolen_total", "steer_pending", "steered_total",
                                      "effort", "effort_source"}
    assert a["shards"][1]["state"] == "PROCESSING" and a["shards"][0]["state"] == "IDLE"
    assert a["shards"][1]["last_channel"] == "55"
    assert len(a["shards"][1]["session_id"]) == 8
    assert a["context_tokens"] == max(s["context_tokens"] for s in a["shards"]) == 9005
    for key, typ in (("name", str), ("context_tokens", int), ("model", str), ("max_turns", int),
                     ("state", str), ("has_discord_token", bool), ("dashboard_chat", bool),
                     ("label", str)):
        assert isinstance(a[key], typ), key
    assert [s["id"] for s in agents["b"]["shards"]] == ["b"]

    ha = hl["agents"]
    assert set(ha) == {"a", "b", "monitor"}
    assert ha["a"]["state"] == "PROCESSING" and ha["a"]["alive"] is True
    assert set(hl["shards"]) == {"a", "a-2", "b", "monitor"}
    for sid in hl["shards"]:
        assert {"state", "alive", "queue_depth", "session_id"} <= set(hl["shards"][sid])
    assert ha["a"]["queue_depth"] == hl["shards"]["a"]["queue_depth"] + hl["shards"]["a-2"]["queue_depth"]
    assert hl["queue_depth"] == sum(v["queue_depth"] for v in hl["shards"].values())


def test_queue_depth_sums(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            for s in ("a", "a-2"):
                h.module.agent_states[s] = "ERROR_RECOVERY"
            await h.send("a", "1")
            await h.send("a-2", "2")
            await h.send("a-2", "3")
            return await get(h, "/health")

    hl = run(scenario())
    assert hl["agents"]["a"]["queue_depth"] == 3
    assert hl["shards"]["a-2"]["queue_depth"] == 2 and hl["queue_depth"] == 3


# -- message routing ---------------------------------------------------------------

def test_message_routing_by_id(harness, caplog):
    h = fixture(harness)

    async def post(payload):
        body = {"content": "x", "server": "local", "author": "t", **payload}
        r = await h.client.post("/message", headers=h._headers(), json=body)
        return r.status

    async def scenario():
        async with h:
            for s in ("a", "a-2", "b"):
                h.module.agent_states[s] = "ERROR_RECOVERY"
            assert await post({"agent": "a"}) == 202             # agent id -> first shard
            assert await post({"agent": "a-2"}) == 202           # shard id as agent
            assert await post({"agent": "a", "shard": "a-2"}) == 202
            assert await post({"agent": "b"}) == 202
            assert await post({"agent": "nope"}) == 400
            assert await post({}) == 400
            with caplog.at_level(logging.WARNING):
                assert await post({"agent": "a", "shard": "ghost"}) == 202  # fallback
            assert await post({"shard": "ghost"}) == 400

    run(scenario())
    assert [r["content"] for r in h.queue_rows("a")] == ["x", "x"]  # agent=a and fallback
    assert len(h.queue_rows("a-2")) == 2 and len(h.queue_rows("b")) == 1


# -- reload / register ----------------------------------------------------------------

def test_reload_adds_a_shard_without_touching_others(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            pids = {s: h.module.agent_processes[s].pid for s in ("a", "a-2", "b")}
            edit(h, lambda d: d["agents"]["a"]["shards"].append({"id": "a-3", "channels": []}))
            r = await h.client.post("/agents/a/reload", headers=h._headers())
            assert r.status == 200, await r.text()
            assert (await r.json())["added"] == ["a-3"]
            assert "a-3" in h.module.agent_processes
            assert {s: h.module.agent_processes[s].pid for s in pids} == pids
            ag = {x["name"]: x for x in (await get(h, "/agents"))["agents"]}
            assert [s["id"] for s in ag["a"]["shards"]] == ["a", "a-2", "a-3"]
            h.script(default={"text": "hi {{env:KARAKOS_SHARD}}"})
            await h.send("a-3", "x")
            await h.wait_idle("a-3")

    run(scenario())
    assert h.queue_rows("a-3")[0]["response"] == "hi a-3"


def test_reload_removes_a_shard(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok", "cost": 0.5})
            await h.send("a-2", "first")
            await h.wait_idle("a-2")
            h.module.agent_states["a-2"] = "ERROR_RECOVERY"
            await h.send("a-2", "q1")
            await h.send("a-2", "q2")
            pid_a = h.module.agent_processes["a"].pid
            edit(h, lambda d: d["agents"]["a"].update(shards=[{"id": "a", "channels": []}]))
            r = await h.client.post("/agents/a/reload", headers=h._headers())
            assert r.status == 200 and (await r.json())["removed"] == ["a-2"]
            assert "a-2" not in h.module.agent_processes
            assert "a-2" not in h.module.agent_states and "a-2" not in h.module.agent_locks
            assert h.module.agent_processes["a"].pid == pid_a
            ag = {x["name"]: x for x in (await get(h, "/agents"))["agents"]}
            assert [s["id"] for s in ag["a"]["shards"]] == ["a"]
            r = await h.client.post("/agents/a-2/kill", headers=h._headers())
            assert r.status == 404

    run(scenario())
    rows = h.queue_rows("a-2")
    queued = [r for r in rows if r["content"] in ("q1", "q2")]
    assert [r["processed"] for r in queued] == [STATUS_SKIPPED] * 2
    assert all(r["response"] == "shard removed" for r in queued)
    assert h._query("SELECT * FROM sessions WHERE agent = 'a-2'")
    assert len(h.cost_rows("a-2")) == 1


def test_invalid_reload_changes_nothing(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            pids = {s: h.module.agent_processes[s].pid for s in ("a", "a-2", "b")}
            edit(h, lambda d: d["agents"]["a"]["shards"].append({"id": "b", "channels": []}))
            r = await h.client.post("/agents/a/reload", headers=h._headers())
            assert r.status == 400
            assert {s: h.module.agent_processes[s].pid for s in pids} == pids
            assert [s.id for s in h.module.shard_specs] == ["a", "a-2", "b", "monitor"]

    run(scenario())


# -- ask ----------------------------------------------------------------------------

def test_ask_uses_the_shard_context(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            heads = h._headers()
            r = await h.client.post("/ask", headers=heads, json={"agent": "ghost", "question": "q"})
            assert r.status == 400
            h.module.agent_turn_context["a-2"] = {"channel_id": "0", "author_ids": []}
            r = await h.client.post("/ask", headers=heads, json={"agent": "a-2", "question": "q"})
            # context found (channel 0 -> 409), not "Invalid agent" (400)
            assert r.status == 409

    run(scenario())


def _tools(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tools_shard_under_test", str(PACKAGE_ROOT / "mcp" / "tools-server.py"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_server_sends_the_shard(monkeypatch, tmp_path):
    tools = _tools(monkeypatch, tmp_path)
    sent = []

    def fake(method, path, payload=None, timeout=15.0):
        sent.append((method, path, payload))
        return 500, {"error": "stop"}

    monkeypatch.setattr(tools, "agent_server_request", fake)
    tools.KARAKOS_AGENT, tools.KARAKOS_SHARD = "a", "a-2"
    tools.ask_user({"question": "q", "options": ["x", "y"]})
    assert sent[-1][2]["agent"] == "a-2"
    tools.KARAKOS_SHARD = ""
    tools.ask_user({"question": "q", "options": ["x", "y"]})
    assert sent[-1][2]["agent"] == "a"


# -- shutdown -----------------------------------------------------------------------

def test_graceful_shutdown_starts_no_summarizer(harness):
    """Step 2.6: shutdown only stops the subprocesses; no summarizer runs."""
    h = fixture(harness)
    marker = h.workspace / "summarizer-ran"

    async def scenario():
        async with h:
            assert not hasattr(h.module, "SUMMARIZE_CMD")
            real = h.module.asyncio.create_subprocess_exec
            spawned = []

            async def spy(*args, **kw):
                spawned.append(args)
                return await real(*args, **kw)

            h.module.asyncio.create_subprocess_exec = spy
            try:
                # Returns normally: no sys.exit inside graceful_shutdown.
                await h.module.graceful_shutdown("TEST")
            finally:
                h.module.asyncio.create_subprocess_exec = real
            assert not h.module.agent_processes
            assert spawned == []

    run(scenario())
    assert not marker.exists()


def test_shutdown_and_exit_orders_shutdown_flush_then_exit_zero(harness):
    """Signal handlers run _shutdown_and_exit: graceful shutdown, then flush
    logging, then os._exit(0). Nothing is raised inside a task, so asyncio
    logs no "Task exception was never retrieved"."""
    h = fixture(harness)
    calls = []

    class _Exited(Exception):
        pass

    async def fake_shutdown(sig):
        calls.append(("shutdown", sig))

    def fake_flush():
        calls.append(("flush",))

    def fake_exit(code):
        calls.append(("exit", code))
        raise _Exited()

    async def scenario():
        async with h:
            with mock.patch.object(h.module, "graceful_shutdown", fake_shutdown), \
                    mock.patch.object(h.module.logging, "shutdown", fake_flush), \
                    mock.patch.object(h.module.os, "_exit", fake_exit):
                with pytest.raises(_Exited):
                    await h.module._shutdown_and_exit("SIGTERM")

    run(scenario())
    assert calls == [("shutdown", "SIGTERM"), ("flush",), ("exit", 0)]


# -- schema -------------------------------------------------------------------------

def test_no_schema_change_and_no_new_migrator_step(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            pass

    run(scenario())
    # which step files exist moves with every migrator step; the fixed order and
    # the "no unlisted step" rule are asserted in tests/migrate/test_chain.py
    steps = sorted(p.name for p in (PACKAGE_ROOT / "lib" / "migrate" / "steps").glob("[0-9][0-9]_*.py"))
    assert "35_rate_limit.py" in steps and "40_memory.py" in steps
    cols = {t: [r["name"] for r in h._query(f"PRAGMA table_info({t})")]
            for t in ("sessions", "message_queue", "cost_events")}
    assert "owner_agent" in cols["message_queue"]  # 1.2's column, not 2.1's
    assert "owner_agent" not in cols["cost_events"]
    assert "shard" not in " ".join(cols["cost_events"] + cols["sessions"])


# -- orphan warning ------------------------------------------------------------------

def test_orphan_key_warning_is_logged_and_boot_continues(harness, caplog):
    h = harness(agents=["a", "b"], shards={"a": ["a-main"]})
    # first boot creates the DB; then seed a 1.x session keyed by the agent's own id

    async def scenario():
        async with h:
            return None

    with caplog.at_level(logging.WARNING):
        run(scenario())
    conn = sqlite3.connect(str(h.module.DB_PATH))
    conn.execute("INSERT OR REPLACE INTO sessions (agent, session_id) VALUES ('a', 'old-sess')")
    conn.commit()
    conn.close()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        run(scenario())
    msgs = [r.getMessage() for r in caplog.records]
    assert ("agent a has data under key a but no shard with that id; "
            "its history will not be used") in msgs
