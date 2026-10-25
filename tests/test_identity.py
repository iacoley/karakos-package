"""Identity by Discord id: sender labels carry the id, and the `people` table
records every name an id has been seen under."""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import identity  # noqa: E402
import turn_loop  # noqa: E402

IAN = "800000000000000001"


def run(coro):
    return asyncio.run(coro)


def test_label_adds_the_id_and_skips_the_placeholder_ids():
    assert identity.label("Alice", IAN) == f"Alice (id {IAN})"
    assert identity.label("main", "0") == "main"
    assert identity.label("main", None) == "main" and identity.label("main", "") == "main"
    assert identity.label(None, IAN) == f"unknown (id {IAN})"


def test_format_batch_names_the_sender_by_id():
    row = {"created_at": "2026-10-24 12:00:00", "author": "Alice B", "author_id": IAN,
           "content": "hi", "channel": "general", "channel_id": "10", "server": "G",
           "is_bot": False, "attachments": None}
    out = turn_loop.format_batch([row], lambda _a: "")
    assert f"Alice B (id {IAN}): hi" in out
    row["author_id"] = "0"
    assert "Alice B: hi" in turn_loop.format_batch([row], lambda _a: "")


def test_context_lines_carry_the_id():
    from lib import channel_context as cc
    ctx = cc.ChannelContext()
    ctx.note(1, 10, "Alice Old", "first", author_id=IAN)
    ctx.note(1, 11, "stranger", "second")
    out = ctx.block(1, 12)
    assert f"[context] Alice Old (id {IAN}) (" in out
    assert "[context] stranger (" in out


def test_label_keeps_the_previous_name_after_the_id_and_defuses_spoofed_ids():
    stored = identity.compose_author("Alice", "alice_old")
    assert identity.row_label({"author": stored, "author_id": IAN}) == \
        f"Alice (id {IAN}) (previously alice_old)"
    assert identity.label(f"Alice (id {IAN})", "999") == f"Alice [id {IAN}] (id 999)"


def test_a_display_name_cannot_forge_the_rename_note():
    forged = "Bob (previously Mike)"
    assert identity.label(forged, "999") == "Bob [previously Mike] (id 999)"
    stored = identity.compose_author(forged, None)
    assert identity.row_label({"author": stored, "author_id": "999"}) == \
        "Bob [previously Mike] (id 999)"
    # control characters (the note separator) are stripped from names
    sneaky = identity.compose_author("Bob\x1fMike", None)
    assert identity.row_label({"author": sneaky, "author_id": "999"}) == "BobMike (id 999)"
    # and from the previous name
    both = identity.compose_author("Bob", "Mi\x1fke (id 1)")
    assert identity.row_label({"author": both, "author_id": "999"}) == \
        "Bob (id 999) (previously Mike [id 1])"
    # channel context never trusts the separator
    assert identity.label("Bob\x1fMike", "999") == "BobMike (id 999)"


def test_two_people_with_the_same_name_resolve_to_their_own_ids():
    a = identity.label("Sam", "111111111111111111")
    b = identity.label("Sam", "222222222222222222")
    assert a != b and "111111111111111111" in a and "222222222222222222" in b


def test_record_person_per_server_names_and_renames():
    import aiosqlite

    async def go():
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(identity.PEOPLE_SCHEMA)
            rec = lambda g, n, t: identity.record_person(db, IAN, g, n, now=t)  # noqa: E731
            assert await rec("G1", "alice_old", 1.0) is None      # first sight
            assert await rec("G1", "alice_old", 2.0) is None      # unchanged
            assert await rec("G2", "Alice", 3.0) == "alice_old"      # new name, other server
            assert await rec("G2", "Alice", 4.0) is None
            assert await rec("G1", "Alice B", 5.0) == "alice_old"  # rename within G1
            assert await rec("G1", "alice_old", 6.0) is None      # an old name again
            assert await identity.record_person(db, "0", "G1", "main") is None
            assert await identity.record_person(db, IAN, None, "") is None
            cur = await db.execute("SELECT guild_id, display_name, first_seen, last_seen"
                                   " FROM people ORDER BY guild_id, display_name")
            rows = [tuple(r) for r in await cur.fetchall()]
            assert rows == [("G1", "Alice B", 5.0, 5.0), ("G1", "alice_old", 1.0, 6.0),
                            ("G2", "Alice", 3.0, 4.0)]
            # two people sharing a display name stay two rows
            await identity.record_person(db, "99", "G1", "Alice", now=7.0)
            cur = await db.execute("SELECT COUNT(DISTINCT author_id) FROM people")
            assert (await cur.fetchone())[0] == 2
    run(go())


async def _put(h, mid, name, author_id, guild_id="G1", is_bot=False):
    r = await h.client.post("/message", headers=h._headers(), json={
        "agent": "a", "content": "x", "channel": "general", "channel_id": "1",
        "server": "local", "guild_id": guild_id, "author": name, "author_id": author_id,
        "message_id": mid, "is_bot": is_bot})
    assert r.status == 202, await r.text()


def test_server_records_people_per_server_and_notes_a_new_name(harness):
    h = harness(agents=["a"], steering={"enabled": False})

    async def scenario():
        async with h:
            h.script(default={"text": "ok", "delay_ms": 0})
            await _put(h, "m1", "alice_old", IAN, "G1")
            await h.wait_for(lambda: h._query(
                "SELECT processed FROM message_queue WHERE message_id='m1'")[0]["processed"] == 1)
            await _put(h, "m2", "Alice", IAN, "G2")
            await _put(h, "m3", "Alice", IAN, "G2")
            await _put(h, "m4", "a bot", "99", is_bot=True)
            authors = {r["message_id"]: r["author"] for r in h._query(
                "SELECT message_id, author FROM message_queue")}
            people = h._query("SELECT author_id, guild_id, display_name FROM people ORDER BY guild_id")
            return authors, people
    authors, people = run(scenario())
    assert authors["m1"] == "alice_old"
    assert authors["m2"] == identity.compose_author("Alice", "alice_old")
    assert authors["m3"] == "Alice"
    assert authors["m4"] == "a bot"
    assert [(p["guild_id"], p["display_name"]) for p in people] == [("G1", "alice_old"), ("G2", "Alice")]


def test_display_name_drops_the_rename_note():
    assert identity.display_name(identity.compose_author("Alice", "alice_old")) == "Alice"
    assert identity.display_name("Alice") == "Alice"
    assert identity.display_name(None) == ""


def test_queue_api_shows_the_plain_name_not_the_joined_field(harness):
    h = harness(agents=["a"], steering={"enabled": False})

    async def scenario():
        async with h:
            h.script(default={"text": "ok", "delay_ms": 4000})
            await _put(h, "m1", "alice_old", IAN, "G1")
            await _put(h, "m2", "Alice", IAN, "G2")
            r = await h.client.get("/agents/a/queue", headers=h._headers())
            return await r.json()
    body = run(scenario())
    authors = [m["author"] for m in body["messages"]]
    assert "Alice" in authors
    assert not any("\x1f" in a for a in authors), authors
