"""Health shows the agent process: pid, uptime, last good turn, and goes
degraded when a shard's process is dead (karakos-128).

`last_turn_at` is in memory only, so it is null until a turn ends without
is_error. Time-based fields are checked for type and range, not exact value.
"""
import asyncio


def run(coro):
    return asyncio.run(coro)


async def get(h, path):
    r = await h.client.get(path, headers=h._headers())
    assert r.status == 200, await r.text()
    return await r.json()


def test_health_reports_pid_uptime_and_null_last_turn(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            body = await get(h, "/health")
            shard = body["agents"]["a"]["shards"]["a"]
            assert isinstance(shard["pid"], int)
            assert isinstance(shard["uptime_seconds"], int) and shard["uptime_seconds"] >= 0
            assert shard["last_turn_at"] is None
            assert body["status"] == "healthy"

    run(scenario())


def test_last_turn_set_after_a_turn(harness):
    h = harness(agents=["a"])
    h.script(default={"text": "ok"})

    async def scenario():
        async with h:
            await h.send("a", "hi")
            await h.wait_idle("a")
            body = await get(h, "/health")
            assert isinstance(body["agents"]["a"]["shards"]["a"]["last_turn_at"], float)

    run(scenario())


def test_dead_agent_makes_status_degraded(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            await h.module.kill_agent_subprocess("a")
            body = await get(h, "/health")
            assert body["status"] == "degraded"
            assert body["agents"]["a"]["shards"]["a"]["pid"] is None

    run(scenario())


def test_agents_route_carries_uptime(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            body = await get(h, "/agents")
            row = next(a for a in body["agents"] if a["name"] == "a")["shards"][0]
            assert {"pid", "uptime_seconds", "last_turn_at"} <= set(row)

    run(scenario())
