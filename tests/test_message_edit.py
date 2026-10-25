"""POST /message/edit (step 6.2): the real server and queue with the fake claude."""
import asyncio

import pytest

SHARDS = {"a": ["a", "a-2"]}
UX = {"channels": {"general": {"id": "1", "ux": {"edit_reroute": True}}}}


# A queued row is edited in place only while it is still queued; with steering on
# (step 2.5) a same-channel row arriving mid-turn is written into the running turn
# within milliseconds and is no longer editable. These tests need it to wait.
NO_STEER = {"enabled": False}


def run(coro):
    return asyncio.run(coro)


async def put(h, mid, text, shard="a", author_id="7", channel="general", author="sam"):
    r = await h.client.post("/message", headers=h._headers(), json={
        "agent": "a", "shard": shard, "content": text, "channel": channel, "channel_id": "1",
        "server": "local", "author": author, "author_id": author_id, "message_id": mid})
    assert r.status == 202, await r.text()


async def edit(h, mid, text, author_id="7"):
    r = await h.client.post("/message/edit", headers=h._headers(), json={
        "server": "local", "message_id": mid, "channel_id": "1", "author_id": author_id,
        "content": text})
    assert r.status == 200, await r.text()
    return await r.json()


def row(h, mid):
    (r,) = h._query("SELECT * FROM message_queue WHERE message_id = ?", (mid,))
    return r


def followups(h, mid):
    return h._query("SELECT * FROM message_queue WHERE message_id LIKE ? ORDER BY id",
                    (f"edit:{mid}:%",))


def busy_script(h, text="{{text}}", delay_ms=700):
    h.script(default={"text": text, "delay_ms": delay_ms})


def test_queued_row_is_updated_in_place(harness):
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h)
            await put(h, "m1", "first")
            await h.wait_for(lambda: row(h, "m1")["processed"] == 1)
            await put(h, "m2", "second")
            assert (await edit(h, "m2", "second, fixed"))["status"] == "updated"
            assert row(h, "m2")["content"] == "second, fixed"
            assert len(h._query("SELECT id FROM message_queue WHERE message_id = 'm2'")) == 1
            assert followups(h, "m2") == []
            await h.wait_idle("a")
    run(scenario())
    assert "second, fixed" in h.sent_to("a")[-1]


def test_thread_row_uses_its_parent_channel_edit_policy(harness):
    """A thread row is labelled "parent/thread"; the parent's config governs it."""
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h)
            await put(h, "m1", "first")
            await h.wait_for(lambda: row(h, "m1")["processed"] == 1)
            await put(h, "m2", "second", channel="general/side-quest")
            assert (await edit(h, "m2", "second, fixed"))["status"] == "updated"
            await h.wait_idle("a")
    run(scenario())


def test_edit_during_the_turn_inserts_a_followup_on_the_same_shard(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h)
            await put(h, "m1", "hello wrld", shard="a-2")
            await h.wait_for(lambda: row(h, "m1")["processed"] == 1)
            res = await edit(h, "m1", "hello world")
            assert res["status"] == "followup"
            (f,) = followups(h, "m1")
            assert f["agent"] == "a-2" and f["message_id"] == "edit:m1:1"
            assert f["channel"] == "general" and f["author_id"] == "7" and f["is_bot"] == 0
            assert f["content"] == ("[sam edited their earlier message while you were working on it]\n"
                                    "Before: hello wrld\nAfter: hello world")
            await h.wait_idle("a-2", timeout=10)
    run(scenario())
    assert row(h, "m1")["content"] == "hello wrld"      # the original is not rewritten


def test_edit_after_an_answer_inserts_a_followup_the_agent_answers(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h, delay_ms=0)
            await put(h, "m1", "what is 2+2")
            await h.wait_idle("a")
            assert (await edit(h, "m1", "what is 3+3"))["status"] == "followup"
            await h.wait_idle("a")
            await h.wait_for(lambda: followups(h, "m1")[0]["response"])
    run(scenario())
    (f,) = followups(h, "m1")
    assert "after you answered it" in f["content"] and f["processed"] == 2
    assert "Before: what is 2+2" in f["response"] and "After: what is 3+3" in f["response"]


def test_edit_after_a_pass_reply_inserts_nothing(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h, text="PASS", delay_ms=0)
            await put(h, "m1", "chatter")
            await h.wait_idle("a")
            await h.wait_for(lambda: row(h, "m1")["response"])
            assert (await edit(h, "m1", "chatter!"))["status"] == "ignored"
    run(scenario())
    assert followups(h, "m1") == []


def test_refusals_change_nothing(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h, delay_ms=0)
            await put(h, "m1", "text")
            await h.wait_idle("a")
            assert (await edit(h, "m1", "text"))["status"] == "unchanged"
            assert (await edit(h, "m1", "other", author_id="99"))["status"] == "author_mismatch"
            assert (await edit(h, "nope", "other"))["status"] == "unknown"
            import sqlite3
            conn = sqlite3.connect(str(h.module.DB_PATH))
            conn.execute("UPDATE message_queue SET created_at = datetime('now', '-1 hour')"
                         " WHERE message_id = 'm1'")
            conn.commit()
            conn.close()
            assert (await edit(h, "m1", "much later"))["status"] == "too_old"
            h.module.channels_config = {"channels": {"general": {"id": "1"}}}
            assert (await edit(h, "m1", "switch off"))["status"] == "disabled"
    run(scenario())
    assert followups(h, "m1") == [] and row(h, "m1")["content"] == "text"


def test_fourth_followup_refused_and_queued_followup_updated_in_place(harness):
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h, delay_ms=0)
            await put(h, "m1", "v0")
            await h.wait_idle("a")
            for i in (1, 2, 3):
                assert (await edit(h, "m1", f"v{i}"))["status"] == "followup"
                await h.wait_idle("a")
            assert len(followups(h, "m1")) == 3
            assert (await edit(h, "m1", "v4"))["status"] == "refused"
            assert len(followups(h, "m1")) == 3

            # While a follow-up is still queued, a further edit rewrites it.
            busy_script(h, delay_ms=700)
            await put(h, "m2", "w0")
            await h.wait_for(lambda: row(h, "m2")["processed"] == 1)
            assert (await edit(h, "m2", "w1"))["status"] == "followup"
            (f,) = followups(h, "m2")
            assert f["processed"] == 0
            assert (await edit(h, "m2", "w2"))["status"] == "updated"
            (f,) = followups(h, "m2")
            assert "Before: w0" in f["content"] and "After: w2" in f["content"]
            await h.wait_idle("a", timeout=10)
    run(scenario())


def test_no_schema_change(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            pass
    run(scenario())
    cols = [r["name"] for r in h._query("PRAGMA table_info(message_queue)")]
    assert cols == ["id", "agent", "channel", "channel_id", "server", "author", "author_id",
                    "is_bot", "content", "message_id", "mentions_agent", "attachments",
                    "processed", "response", "discord_response_id", "created_at",
                    "processing_started_at", "processed_at", "not_before", "call_id",
                    "reply_to_agent", "priority", "expires_at", "depth", "partial_response",
                    "restart_count", "claimed_by", "owner_agent"]


def test_repeated_reaction_notice_is_the_servers_duplicate(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.script(default={"text": "PASS"})
            body = {"agent": "a", "shard": "a", "content": "[reaction] ...", "channel_id": "1",
                    "server": "local", "author": "sam", "message_id": "reaction:9:7:abcd1234"}
            r1 = await h.client.post("/message", headers=h._headers(), json=body)
            r2 = await h.client.post("/message", headers=h._headers(), json=body)
            assert r1.status == 202 and (await r1.json())["status"] == "queued"
            assert r2.status == 202 and (await r2.json())["status"] == "duplicate"
            await h.wait_idle("a")
    run(scenario())


def test_edit_of_a_steered_message_is_a_followup_that_runs_next(harness):
    """Steering ON (the default). A same-channel message that arrives mid-turn is
    steered into the running turn, so by edit time the CLI has already read it.
    The edit must not rewrite that row (the CLI would never see it) and must not
    be lost: it becomes an `edit:<id>:<n>` follow-up row that runs as its own,
    next turn (steerable() and claim_steerable refuse `edit:` rows)."""
    h = harness(agents=["a", "b"], shards=SHARDS, steering={"coalesce_ms": 0})

    async def scenario():
        async with h:
            h.module.channels_config = UX
            h.script(rules=[
                {"match": "first", "step": {"text": "r-first", "tools": [
                    {"name": "Bash", "input": {"command": "sleep"}, "ms": 900}]}},
                {"match": "edited", "step": {"text": "r-edit"}}])
            await put(h, "m1", "first")
            await h.wait_for(lambda: row(h, "m1")["processed"] == 1)
            await asyncio.sleep(0.2)
            await put(h, "m2", "second")
            await h.wait_for(lambda: h.module.STATE.steered_total.get("a") == 1)
            assert row(h, "m2")["processed"] == 1                # steered: already read
            res = await edit(h, "m2", "second, edited")
            assert res == {"status": "followup", "message_id": "m2",
                           "followup_id": "edit:m2:1"}
            assert row(h, "m2")["content"] == "second"           # never rewritten
            (f,) = followups(h, "m2")
            assert f["agent"] == "a" and "second, edited" in f["content"]
            await h.wait_idle("a", timeout=10)
            assert row(h, "edit:m2:1")["processed"] == 2          # not lost
            assert row(h, "edit:m2:1")["response"] == "r-edit"
    run(scenario())
    assert len(h.results("a")) == 2                               # its own turn
    assert h.module.STATE.steered_total.get("a") == 1             # only m2 was steered
    assert "second, edited" in h.sent_to("a")[-1]                 # the model saw it
    assert row(h, "m2")["processed"] == 2 and row(h, "m1")["processed"] == 2


def test_followup_names_the_author_without_the_rename_note(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.module.channels_config = UX
            busy_script(h)
            await put(h, "m0", "first", shard="a-2", author="alice_old")
            await h.wait_for(lambda: row(h, "m0")["processed"] == 1)
            await put(h, "m1", "hello wrld", shard="a-2", author="Alice")   # a renamed sender
            assert "\x1f" in row(h, "m1")["author"]
            await h.wait_for(lambda: row(h, "m1")["processed"] == 1)
            assert (await edit(h, "m1", "hello world"))["status"] == "followup"
            (f,) = followups(h, "m1")
            assert f["content"].startswith("[Alice edited their earlier message")
            assert "\x1f" not in f["content"]
            await h.wait_idle("a-2", timeout=10)
    run(scenario())
