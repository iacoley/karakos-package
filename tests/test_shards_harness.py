"""Shards end to end: real agent-server, fake `claude` (step 2.1).

Fixture: agent `a` with shards `a` and `a-2`, agent `b` with its default shard.
"""

import asyncio
import json

SHARDS = {"a": ["a", "a-2"]}


def run(coro):
    return asyncio.run(coro)


def fixture(harness):
    return harness(agents=["a", "b"], shards=SHARDS)


def sysprompt(h, shard):
    argv = h.argv(shard)
    return argv[argv.index("--system-prompt") + 1]


def appended(h, shard):
    argv = h.argv(shard)
    return argv[argv.index("--append-system-prompt") + 1] if "--append-system-prompt" in argv else ""


def pid(h, shard):
    return h.module.agent_processes[shard].pid


def last_row(h, shard):
    return h.queue_rows(shard)[-1]


# -- spawn --------------------------------------------------------------------

def test_spawn_three_subprocesses_with_distinct_sessions(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "{{env:KARAKOS_SHARD}}/{{env:KARAKOS_AGENT}}"})
            for s in ("a", "a-2", "b"):
                await h.send(s, "hi")
            for s in ("a", "a-2", "b"):
                await h.wait_idle(s)
            assert {"a", "a-2", "b"} <= set(h.module.agent_processes)
            assert h.module.STATE.shard_ids()[:3] == ["a", "a-2", "b"]

    run(scenario())
    sids = [h.session_id(s) for s in ("a", "a-2", "b")]
    assert len(set(sids)) == 3
    for s, sid in zip(("a", "a-2", "b"), sids):
        argv = h.argv(s)
        assert argv[argv.index("--session-id") + 1] == sid
    assert last_row(h, "a")["response"] == "a/a"
    assert last_row(h, "a-2")["response"] == "a-2/a"
    assert last_row(h, "b")["response"] == "b/b"


def test_prompt_differs_by_shard_file_only_and_persona_is_shared(harness):
    h = fixture(harness)
    ws = h.workspace
    (ws / "agents/a/persona").mkdir(parents=True, exist_ok=True)
    (ws / "agents/a/persona/p.md").write_text("PERSONA-A")
    (ws / "agents/a/memory").mkdir(parents=True, exist_ok=True)
    (ws / "agents/a/memory/MEMORY.md").write_text("MEMORY-INDEX-A")
    (ws / "agents/a/shards").mkdir(parents=True, exist_ok=True)
    (ws / "agents/a/shards/a-2.md").write_text("SHARD-TWO-TEXT")

    async def scenario():
        async with h:
            for s in ("a", "a-2"):
                await h.send(s, "x")
                await h.wait_idle(s)

    run(scenario())
    p1, p2 = sysprompt(h, "a"), sysprompt(h, "a-2")
    assert "SHARD-TWO-TEXT" in p2 and "SHARD-TWO-TEXT" not in p1
    assert p2 == p1 + "\n\nSHARD-TWO-TEXT"  # differs exactly by the shard file
    assert appended(h, "a") == appended(h, "a-2")
    assert "PERSONA-A" in appended(h, "a") and "MEMORY-INDEX-A" in appended(h, "a")
    assert (ws / "agents/a/shards/a-2.generated.md").exists()
    assert (ws / "agents/a/SYSTEM_PROMPT.generated.md").exists()


def test_onboarding_only_in_first_shard(harness):
    h = fixture(harness)
    ws = h.workspace
    (ws / "agents/a/onboarding.md").write_text("ONBOARD {{AGENT_NAME}}")

    async def scenario():
        async with h:
            for s in ("a", "a-2"):
                await h.send(s, "x")
                await h.wait_idle(s)

    run(scenario())
    assert "ONBOARD a" in appended(h, "a")
    assert "ONBOARD" not in appended(h, "a-2")


# -- concurrency and isolation -------------------------------------------------

def test_shards_run_concurrently_and_stay_isolated(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "re: {{text}}"}, rules=[
                {"match": "slow", "agent": "^a$", "step": {"text": "slow done", "delay_ms": 800}}])
            await h.send("a", "slow one", channel_id="10")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")
            await h.send("a-2", "quick two", channel_id="20")
            await h.wait_idle("a-2")
            assert h.module.agent_states["a"] == "PROCESSING"
            posted = [d for d in h.discord if d["channel_id"] == "20"]
            assert posted and posted[-1]["agent"] == "a-2"
            assert not [d for d in h.discord if d["channel_id"] == "10"]
            await h.send("b", "bee")
            await h.wait_idle("b")
            await h.wait_idle("a")

    run(scenario())
    assert len(h.queue_rows("a-2")) == 1 and len(h.queue_rows("a")) == 1
    assert "quick two" in h.sent_to("a-2")[0] and len(h.sent_to("a-2")) == 1
    assert all("quick two" not in t for t in h.sent_to("a"))
    assert h.module.agent_states["a"] == h.module.agent_states["a-2"] == "IDLE"


# -- cost and session rows -------------------------------------------------------

def usage(cr):
    return {"input_tokens": 5, "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": cr, "output_tokens": 7}


def test_cost_and_session_rows_are_per_shard(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            for s, cr, cost in (("a", 1000, 0.5), ("a-2", 5000, 0.25), ("b", 200, 0.125)):
                h.script(default={"text": "ok", "usage": usage(cr), "cost": cost})
                await h.send(s, "go")
                await h.wait_idle(s)
            heads = h._headers()
            r = await (await h.client.get("/cost/a", headers=heads)).json()
            r2 = await (await h.client.get("/cost/a-2", headers=heads)).json()
            allc = await (await h.client.get("/cost", headers=heads)).json()
            return r, r2, allc

    r, r2, allc = run(scenario())
    for s in ("a", "a-2", "b"):
        rows = h.cost_rows(s)
        assert len(rows) == 1 and rows[0]["agent"] == s
    assert len(h.cost_rows()) == 3
    ctx = {s: h._query("SELECT context_tokens FROM sessions WHERE agent = ?", (s,))[0]["context_tokens"]
           for s in ("a", "a-2", "b")}
    assert ctx == {"a": 1105, "a-2": 5105, "b": 305}
    d = {s: h.cost_rows(s)[0]["cost_delta"] for s in ("a", "a-2", "b")}
    assert abs(r["daily"] - (d["a"] + d["a-2"])) < 1e-9
    assert set(r["shards"]) == {"a", "a-2"}
    assert abs(r2["daily"] - d["a-2"]) < 1e-9 and "shards" not in r2
    assert set(allc["daily"]) == {"a", "a-2", "b"}
    by = allc["by_agent"]["daily"]
    assert set(by) == {"a", "b"}
    assert abs(by["a"] - (d["a"] + d["a-2"])) < 1e-9 and abs(by["b"] - d["b"]) < 1e-9


def test_post_cost_accepts_shard_or_agent_id(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            heads = h._headers()
            for name in ("a-2", "a", "b"):
                r = await h.client.post("/cost", headers=heads, json={"agent": name, "cost_delta": 1.0})
                assert r.status == 200
            r = await h.client.post("/cost", headers=heads, json={"agent": "zz", "cost_delta": 1.0})
            assert r.status == 400

    run(scenario())
    assert [len(h.cost_rows(s)) for s in ("a", "a-2", "b")] == [1, 1, 1]


# -- reset, interrupt, kill -------------------------------------------------------

def test_reset_targets(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            heads = h._headers()
            ids = lambda: {s: h.session_id(s) for s in ("a", "a-2", "b")}
            before = ids()
            await h.client.post("/agents/a-2/reset", headers=heads)
            s1 = ids()
            assert s1["a-2"] != before["a-2"] and s1["a"] == before["a"] and s1["b"] == before["b"]
            r = await h.client.post("/agents/a/reset", headers=heads)
            body = await r.json()
            assert body["shards"] == ["a", "a-2"]
            s2 = ids()
            assert s2["a"] != s1["a"] and s2["a-2"] != s1["a-2"] and s2["b"] == s1["b"]
            await h.client.post("/agents/a/reset?shard=a", headers=heads)
            s3 = ids()
            assert s3["a"] != s2["a"] and s3["a-2"] == s2["a-2"]
            r = await h.client.post("/agents/a/reset?shard=b", headers=heads)
            assert r.status == 404

    run(scenario())


def test_interrupt_and_kill(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            heads = h._headers()
            h.script(default={"text": "ok"}, rules=[
                {"match": "stuck", "agent": "^a-2$", "step": {"hang": True}}])
            pid_a = pid(h, "a")
            await h.send("a-2", "stuck")
            await h.wait_for(lambda: h.module.agent_states.get("a-2") == "PROCESSING")
            await h.wait_for(lambda: h.sent_to("a-2"))
            r = await (await h.client.post("/agents/a/interrupt", headers=heads)).json()
            assert r["interrupted"] is True and r["shards"] == ["a-2"]
            await h.wait_idle("a-2")
            assert pid(h, "a") == pid_a
            r = await (await h.client.post("/agents/a/interrupt", headers=heads)).json()
            assert r["interrupted"] is False and r["status"] == "idle"

            r = await (await h.client.post("/agents/a/kill?shard=a-2", headers=heads)).json()
            assert r["was_running"] is True and "a-2" not in h.module.agent_processes
            assert "a" in h.module.agent_processes
            r = await (await h.client.post("/agents/a/kill", headers=heads)).json()
            assert r["was_running"] is True and r["shards"] == ["a", "a-2"]
            assert "a" not in h.module.agent_processes and "b" in h.module.agent_processes
            r = await (await h.client.post("/agents/a/kill", headers=heads)).json()
            assert r["was_running"] is False
            r = await h.client.post("/agents/nope/kill", headers=heads)
            assert r.status == 404

    run(scenario())


def test_queue_flush_and_delete_across_shards(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            heads = h._headers()
            # park both shards so rows stay queued
            for s in ("a", "a-2"):
                h.module.agent_states[s] = "ERROR_RECOVERY"
            await h.send("a", "one")
            await h.send("a-2", "two")
            await h.send("a-2", "three")
            q = await (await h.client.get("/agents/a/queue", headers=heads)).json()
            assert [m["content"] for m in q["messages"]] == ["one", "two", "three"]
            assert [m["agent"] for m in q["messages"]] == ["a", "a-2", "a-2"]
            q2 = await (await h.client.get("/agents/a-2/queue", headers=heads)).json()
            assert [m["content"] for m in q2["messages"]] == ["two", "three"]
            q3 = await (await h.client.get("/agents/a/queue?shard=a", headers=heads)).json()
            assert [m["content"] for m in q3["messages"]] == ["one"]
            r = await h.client.delete(f"/agents/a/queue/{q['messages'][1]['id']}", headers=heads)
            assert r.status == 200
            r = await (await h.client.post("/agents/a/flush", headers=heads)).json()
            assert r["flushed"] == 2 and r["shards"] == ["a", "a-2"]

    run(scenario())


# -- respawn --------------------------------------------------------------------

def test_respawn_only_the_crashed_shard(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                {"match": "boom", "agent": "^a-2$", "step": {"text": "dying", "exit": True}}])
            pid_a = pid(h, "a")
            await h.send("a-2", "boom", channel_id="77")
            await h.wait_for(lambda: any("restarted" in d["content"] for d in h.discord))
            await h.wait_idle("a-2")
            assert pid(h, "a") == pid_a
            await h.send("a", "still here")
            await h.wait_idle("a")

    run(scenario())
    notice = [d for d in h.discord if "restarted" in d["content"]]
    assert len(notice) == 1
    assert "a (a-2) restarted" in notice[0]["content"]
    assert notice[0]["channel_id"] == "77" and notice[0]["agent"] == "a-2"
    assert last_row(h, "a")["response"] == "ok"


def test_token_registered_under_each_shard_id(harness, monkeypatch):
    h = harness(agents={"a": {"discord": {"token_env": "TOK_A"}}, "b": {}},
                shards=SHARDS)
    monkeypatch.setenv("TOK_A", "secret-a")

    async def scenario():
        async with h:
            await h.client.post("/agents/a/reload", headers=h._headers())
            t = h.module.AGENT_TOKENS
            assert t["a"] == t["a-2"] == "secret-a" and "b" not in t

    run(scenario())


def test_crashloop_brake_is_per_shard(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            # This test is about the 4-exit crashloop brake, not resume. The first
            # message marks a-2's session started, so every respawn would --resume
            # and enter the resume fallback. Give each a-2 spawn a cold start: clear
            # its stored session first (fresh id, not started), as a cold start does.
            real_get_or_create = h.module.get_or_create_session

            async def cold_session(agent):
                if agent == "a-2":
                    await h.module.clear_session(agent)
                return await real_get_or_create(agent)

            h.module.get_or_create_session = cold_session
            try:
                h.script(default={"text": "ok"}, rules=[
                    {"match": "boom", "agent": "^a-2$", "step": {"text": "x", "exit": True}}])
                for i in range(4):
                    await h.send("a-2", "boom", channel_id="77")
                    await h.wait_for(
                        lambda i=i: len([d for d in h.discord
                                         if "restarted" in d["content"] or "is down" in d["content"]]) >= i + 1,
                        timeout=10)
                    if i < 3:
                        await h.wait_idle("a-2")
                assert h.module.agent_states["a-2"] != "IDLE" or "a-2" not in h.module.agent_processes \
                    or h.module.agent_processes["a-2"].returncode is not None
                assert any("a (a-2) is down" in d["content"] for d in h.discord)
                await h.send("a", "alive?")
                await h.wait_idle("a")
            finally:
                h.module.get_or_create_session = real_get_or_create

    run(scenario())
    assert last_row(h, "a")["response"] == "ok"


# -- a 1.x install with no shards ------------------------------------------------

def test_no_shards_install_is_unchanged(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            assert h.module.STATE.shard_ids() == ["a", "b", "monitor"]
            assert set(h.module.agent_processes) == {"a", "b", "monitor"}
            heads = h._headers()
            ag = await (await h.client.get("/agents", headers=heads)).json()
            hl = await (await h.client.get("/health", headers=heads)).json()
            return ag, hl

    ag, hl = run(scenario())
    a = {x["name"]: x for x in ag["agents"]}["a"]
    assert [s["id"] for s in a["shards"]] == ["a"] and a["shards"][0]["is_default"]
    for key in ("name", "context_tokens", "model", "max_turns", "timeout", "state",
                "has_discord_token", "dashboard_chat", "label"):
        assert key in a
    assert set(hl["agents"]["a"]) >= {"state", "alive", "queue_depth", "session_id"}
    assert set(hl["shards"]) == {"a", "b", "monitor"}


def test_old_style_state_without_a_registry(harness):
    """A test that sets agent_config directly and never loads a registry."""
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            m = h.module
            saved, specs = m.agent_config, m.shard_specs
            try:
                m.shard_specs = []
                m.agent_config = {"x": {"model": "m"}}
                st = m.STATE
                assert st.shard_ids() == ["x"] and st.agent_of("x") == "x"
                assert st.cfg("x") == {"model": "m"}
            finally:
                m.agent_config, m.shard_specs = saved, specs

    run(scenario())
