"""Interrupt with a message (step 2.5): a control_request ends the turn, the
process stays alive, and the message runs next at INTERRUPT_PRIORITY."""

import asyncio

SHARDS = {"a": ["a", "a-2"]}


def run(coro):
    return asyncio.run(coro)


def fixture(harness):
    return harness(agents=["a", "b"], shards=SHARDS, steering={"coalesce_ms": 0})


def long_tool(text="r-A", ms=5000):
    return {"text": text, "tools": [{"name": "Bash", "input": {"command": "sleep"}, "ms": ms}]}


def row(h, shard, text):
    return next(r for r in h.queue_rows(shard) if r["content"] == text)


async def post_interrupt(h, name, body=None, shard=None):
    url = f"/agents/{name}/interrupt" + (f"?shard={shard}" if shard else "")
    resp = await h.client.post(url, headers=h._headers(), **({"json": body} if body else {}))
    return resp.status, await resp.json()


def test_interrupt_with_message_same_process(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": long_tool()},
                    {"match": "urgent", "step": {"text": "r-urgent"}}])

    async def scenario():
        async with h:
            pid = h.module.agent_processes["a-2"].pid  # untouched sibling
            pid_a = h.module.agent_processes["a"].pid
            await h.send("a", "A-msg")
            await h.wait_for(lambda: row(h, "a", "A-msg")["processed"] == 1)
            await asyncio.sleep(0.3)
            status, body = await post_interrupt(
                h, "a", {"message": "urgent please", "channel_id": "1", "author": "mike"},
                shard="a")
            assert status == 200 and body["interrupted"] is True
            await h.wait_idle("a", timeout=10)

            ev = h.stdin_events("a")
            kinds = [(e["event"].get("type"), e["event"].get("request", {}).get("subtype")
                      if e["event"].get("type") == "control_request" else None) for e in ev]
            ci = kinds.index(("control_request", "interrupt"))
            mi = next(i for i, e in enumerate(ev) if e["event"].get("type") == "user"
                      and "urgent" in str(e["event"]))
            assert ci < mi
            aborted = next(r for r in h.io("a") if r.get("dir") == "out"
                           and r["event"].get("type") == "result")
            assert aborted["event"]["subtype"] == "error_during_execution"
            assert ev[mi]["t"] >= aborted["t"]            # written only after the aborted result

            assert [r["subtype"] for r in h.results("a")] == ["error_during_execution", "success"]
            assert h.module.agent_processes["a"].pid == pid_a          # no kill
            assert h.module.agent_processes["a-2"].pid == pid
            assert not any(r.get("signal") for r in h.io("a"))         # no signal recorded

            # The aborted turn's row ends as an interrupted turn's row does today:
            # complete, no reply text, nothing posted for it.
            a_row = row(h, "a", "A-msg")
            assert (a_row["processed"], a_row["response"]) == (2, "")
            m_row = row(h, "a", "urgent please")
            assert (m_row["processed"], m_row["response"], m_row["priority"]) == (2, "r-urgent", 100)
            assert [d["content"] for d in h.discord if not d["content"].startswith("-#")] \
                == ["r-urgent"]
            assert "a" not in h.module.interrupted_agents
    run(scenario())


def test_interrupt_message_is_claimed_before_queued_rows(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": long_tool()},
                    {"match": ".", "step": {"text": "ok"}}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg", channel_id="1")
            await h.wait_for(lambda: row(h, "a", "A-msg")["processed"] == 1)
            await h.send("a", "other-channel", channel_id="2")     # waits: other channel
            await asyncio.sleep(0.2)
            await post_interrupt(h, "a", {"message": "urgent", "channel_id": "1"}, shard="a")
            await h.wait_idle("a", timeout=10)
            # A batch holds one channel: the priority row's turn leaves the
            # other channel's row for its own turn.
            batch_text = next(t for t in h.sent_to("a") if "urgent" in t)
            assert "other-channel" not in batch_text
            assert any("other-channel" in t for t in h.sent_to("a"))
            assert row(h, "a", "other-channel")["processed"] == 2
    run(scenario())


def test_interrupt_message_to_idle_shard_just_runs(harness):
    h = fixture(harness)
    h.script(default={"text": "r"})

    async def scenario():
        async with h:
            status, body = await post_interrupt(h, "a", {"message": "hello"}, shard="a")
            assert status == 200 and body["interrupted"] is False
            await h.wait_idle("a")
            assert row(h, "a", "hello")["processed"] == 2
    run(scenario())


def test_plain_interrupt_keeps_its_response_and_kills(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": long_tool()}])

    async def scenario():
        async with h:
            pid = h.module.agent_processes["a"].pid
            await h.send("a", "A-msg")
            await h.wait_for(lambda: row(h, "a", "A-msg")["processed"] == 1)
            await asyncio.sleep(0.2)
            status, body = await post_interrupt(h, "a", shard="a")
            assert (status, set(body)) == (200, {"status", "interrupted"})
            assert body == {"status": "interrupted", "interrupted": True}
            assert h.module.agent_processes["a"].pid != pid      # today's kill-and-respawn
            status, body = await post_interrupt(h, "b")
            assert body == {"status": "idle", "interrupted": False}
    run(scenario())


def test_no_control_response_falls_back_to_kill(harness, monkeypatch):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": {"hang": True}},
                    {"match": "urgent", "step": {"text": "r-urgent"}}])

    async def scenario():
        async with h:
            monkeypatch.setattr(h.module.turn_loop, "CONTROL_TIMEOUT_S", 0.5)
            pid = h.module.agent_processes["a"].pid
            await h.send("a", "A-msg")
            await h.wait_for(lambda: row(h, "a", "A-msg")["processed"] == 1)
            await asyncio.sleep(0.2)
            status, body = await post_interrupt(h, "a", {"message": "urgent"}, shard="a")
            assert status == 200 and body["interrupted"] is True
            await h.wait_for(lambda: row(h, "a", "urgent")["processed"] == 2, timeout=10)
            assert h.module.agent_processes["a"].pid != pid
            assert row(h, "a", "urgent")["response"] == "r-urgent"
            await h.wait_idle("a")
            assert h.module.agent_states["a"] == "IDLE"
    run(scenario())


def test_message_with_several_target_shards_is_400(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            status, body = await post_interrupt(h, "a", {"message": "urgent"})
            assert (status, body) == (400, {"error": "shard required"})
            assert h.queue_rows("a") == [] and h.queue_rows("a-2") == []
    run(scenario())


def test_message_interrupt_with_steering_off_still_interrupts(harness):
    """enabled: false restores 2.0 exactly: any interrupt kills the turn, and the
    message then runs first on the respawned process."""
    h = harness(agents=["a", "b"], shards=SHARDS, steering={"enabled": False})
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 4000}},
                    {"match": "urgent", "step": {"text": "r-urgent"}}])

    async def scenario():
        async with h:
            pid = h.module.agent_processes["a"].pid
            await h.send("a", "A-msg")
            await h.wait_for(lambda: row(h, "a", "A-msg")["processed"] == 1)
            await asyncio.sleep(0.2)
            status, body = await post_interrupt(h, "a", {"message": "urgent"}, shard="a")
            assert status == 200 and body["interrupted"] is True
            await h.wait_for(lambda: row(h, "a", "urgent")["processed"] == 2, timeout=10)
            assert h.module.agent_processes["a"].pid != pid
            assert row(h, "a", "urgent")["response"] == "r-urgent"
    run(scenario())
