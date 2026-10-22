"""Recent channel messages reach the agent with a delivery (#276)."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import channel_context as cc

RELAY_PATH = Path(__file__).parent.parent / "bin" / "relay.py"


def ring(*msgs):
    c = cc.ChannelContext()
    for i, (author, text, *own) in enumerate(msgs, 1):
        c.note(7, 100 + i, author, text, own=bool(own and own[0]), ts=1_700_000_000 + i * 60)
    return c


def test_mention_after_three_unmentioned_messages_carries_all_three_in_order():
    c = ring(("Ian", "the draft is wrong"), ("Mike", "agreed"), ("Amos", "I'll look"), ("Ian", "@marvin take this"))
    lines = c.block(7, 104).split("\n")
    assert [ln.split(":")[0].split(" (")[0] for ln in lines] == [
        "[context] Ian", "[context] Mike", "[context] Amos"]
    assert lines[0].endswith(": the draft is wrong") and "(22:14)" in lines[0]
    assert "take this" not in c.block(7, 104)    # the triggering message is not repeated


def test_the_agents_own_post_resets_the_window():
    c = ring(("Ian", "old"), ("Marvin", "my reply", True), ("Mike", "new one"), ("Ian", "@marvin now"))
    out = c.block(7, 104)
    assert "new one" in out and "old" not in out and "my reply" not in out


def test_limit_zero_is_the_bare_message_and_limit_caps_with_a_note():
    c = ring(*[("Ian", f"m{i}") for i in range(6)], ("Ian", "trigger"))
    assert c.block(7, 107, 0) == ""
    out = c.block(7, 107, 2).split("\n")
    assert out[0] == cc.MORE and out[1].endswith("m4") and out[2].endswith("m5")


def test_a_full_ring_without_an_own_post_says_it_was_cut_short():
    c = ring(*[("Ian", f"m{i}") for i in range(cc.RING + 5)])
    assert c.block(7, "nope", 20).split("\n")[0] == cc.MORE


def test_other_agents_posts_are_included_text_is_one_line_and_capped():
    c = cc.ChannelContext()
    c.note(7, 1, "Amos", "line one\n\nline   two " + "x" * 500, ts=1_700_000_000)
    [line] = c.block(7, 2).split("\n")
    assert line.startswith("[context] Amos (") and "\n" not in line
    assert len(line.split(": ", 1)[1]) == cc.TEXT_CAP and "line one line two" in line


def test_channels_are_separate_and_empty_is_empty():
    c = ring(("Ian", "x"))
    assert c.block(8, 1) == "" and cc.ChannelContext().block(7, 1) == ""


@pytest.mark.parametrize("cfg,want", [
    ({}, 8), (None, 8), ({"context_messages": 0}, 0), ({"context_messages": 20}, 20),
    ({"context_messages": 21}, 8), ({"context_messages": -1}, 8), ({"context_messages": True}, 8),
    ({"context_messages": "5"}, 8)])
def test_context_limit_reads_the_channel_level_key(cfg, want):
    assert cc.context_limit(cfg) == want


# -- through the relay ------------------------------------------------------------

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture
def relay(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("relay_ctx_under_test", RELAY_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["relay_ctx_under_test"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return mod


def _deliver(relay, context, channel_cfg, trigger_text="@marvin take this"):
    cls = next(v for v in vars(relay).values()
               if isinstance(v, type) and hasattr(v, "send_to_agent_server"))
    sent = []

    class Resp:
        status = 202

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Http:
        def post(self, url, json=None, headers=None):
            sent.append(json)
            return Resp()

    async def none(*a, **k):
        return None

    async def no_attach(m):
        return []

    me = SimpleNamespace(
        channel_context=context, http_session=Http(),
        resolve_channel=lambda ch: ("debug", None), download_attachments=no_attach,
        resolve_reply_context=none, get_channel_name=lambda c: "debug")
    relay.channels_config = {"channels": {"debug": channel_cfg}}
    msg = SimpleNamespace(
        id=104, content=trigger_text, channel=SimpleNamespace(id=7, name="debug", parent=None),
        author=SimpleNamespace(display_name="Ian", id=1, bot=False), guild=None,
        mentions=[], role_mentions=[], reference=None)
    route = SimpleNamespace(agent="marvin", shard="marvin")
    asyncio.run(cls.send_to_agent_server(me, msg, route))
    return sent[0]["content"]


def test_relay_prepends_the_context_block(relay):
    c = ring(("Ian", "the draft is wrong"), ("Mike", "agreed"), ("Amos", "ok"))
    out = _deliver(relay, c, {})
    assert out.endswith("\n@marvin take this")
    assert out.count("[context]") == 3 and out.index("draft is wrong") < out.index("agreed")


def test_relay_context_messages_zero_delivers_the_bare_message(relay):
    c = ring(("Ian", "the draft is wrong"))
    assert _deliver(relay, c, {"context_messages": 0}) == "@marvin take this"
