"""format_batch (lib/turn_loop.py) names where each queued row was posted.

The location tag is printed on the first row and on any row whose tag differs
from the row before it, so a same-channel batch carries it once.
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import turn_loop  # noqa: E402


def _row(author, content, channel="general", channel_id="10", server="Guild"):
    return {"created_at": "2026-10-19 12:00:00", "author": author, "content": content,
            "channel": channel, "channel_id": channel_id, "server": server,
            "is_bot": False, "attachments": None}


def _fmt(rows):
    return turn_loop.format_batch(rows, lambda _a: "")


def test_tag_shown_on_first_row():
    got = _fmt([_row("alice", "hi")])
    assert got == "[2026-10-19 12:00:00] [#general (channel 10, server Guild)] alice: hi"


def test_same_channel_second_row_has_no_tag():
    got = _fmt([_row("alice", "one"), _row("bob", "two")])
    assert got == ("[2026-10-19 12:00:00] [#general (channel 10, server Guild)] alice: one"
                   "\n\n[2026-10-19 12:00:00] bob: two")


def test_channel_change_adds_a_tag():
    got = _fmt([_row("alice", "one"),
                _row("bob", "two", channel="kitchen", channel_id="20")])
    lines = got.split("\n\n")
    assert lines[0].endswith("alice: one")
    assert lines[1] == ("[2026-10-19 12:00:00] [#kitchen (channel 20, server Guild)]"
                        " bob: two")


def test_tag_returns_when_the_channel_switches_back():
    got = _fmt([_row("a", "1"), _row("b", "2", channel="kitchen", channel_id="20"),
                _row("c", "3")])
    first, second, third = got.split("\n\n")
    assert "#general (channel 10" in first
    assert "#kitchen (channel 20" in second
    assert "#general (channel 10" in third


def test_unknown_channel_falls_back_to_the_id_form():
    got = _fmt([_row("alice", "hi", channel="unknown", channel_id="99", server="dm")])
    assert got == "[2026-10-19 12:00:00] [channel 99, server dm] alice: hi"


def test_empty_channel_falls_back_to_the_id_form():
    got = _fmt([_row("alice", "hi", channel="", channel_id="99", server="Guild")])
    assert got == "[2026-10-19 12:00:00] [channel 99, server Guild] alice: hi"


def test_internal_local_rows_keep_the_tag_format():
    got = _fmt([_row("harness", "hi", channel="general", channel_id="1", server="local")])
    assert got == "[2026-10-19 12:00:00] [#general (channel 1, server local)] harness: hi"


def test_all_bot_batch_keeps_the_automated_sentinel():
    got = turn_loop.format_batch([dict(_row("bot", "x"), is_bot=True)],
                                 lambda _a: "", sentinel="SENTINEL")
    assert got.startswith("SENTINEL\n[2026-10-19 12:00:00] [#general")
