"""Relay routing with stub Discord messages (step 2.2).

Same pattern as test_relay_reply_gate.py: no Discord connection, the adapter
records what it would send to the agent server.
"""

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")

A_BOT, B_BOT, OTHER_BOT, MIKE = 111, 222, 999, 1
CH = {"general": 1001, "ops": 1002, "b-room": 1003, "lobby": 1004}
UNLISTED = 4040

AGENTS = {
    "version": 2,
    "agents": {
        "a": {"name": "a", "role": "primary", "shards": [
            {"id": "a", "channels": ["general"]},
            {"id": "a-2", "channels": ["ops"]}]},
        "b": {"name": "b", "role": "custom", "shards": [
            {"id": "b", "channels": ["b-room"]}]},
        "m": {"name": "m", "role": "monitor"},
    },
}


def channels_json(**extra):
    return {"server_id": "42", "channels": {
        n: {"id": str(i), **extra.get(n, {})} for n, i in CH.items()}}


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_routing_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_routing_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


class Author:
    def __init__(self, i, bot=False):
        self.id, self.bot, self.display_name = i, bot, f"u{i}"

    def __eq__(self, o):
        return getattr(o, "id", object()) == self.id

    def __hash__(self):
        return hash(self.id)


class Mention:
    def __init__(self, i, bot=True):
        self.id, self.bot = i, bot


class Msg:
    def __init__(self, channel_id, content="hello", author=MIKE, bot=False, mentions=()):
        self.author = Author(author, bot)
        self.channel = type("C", (), {"id": channel_id, "send": None})()
        self.content = content
        self.id = 5555
        self.mentions = list(mentions)
        self.reference = None
        self.guild = type("G", (), {"id": 42, "name": "Test Guild"})()


@pytest.fixture
def workspace(relay, tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(yaml.safe_dump(AGENTS, sort_keys=False))
    (tmp_path / "config" / "channels.json").write_text(json.dumps(channels_json()))
    monkeypatch.setattr(relay, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(relay, "AGENTS_CONFIG_PATH", tmp_path / "config" / "agents.yaml")
    monkeypatch.setattr(relay, "CHANNELS_CONFIG_PATH", tmp_path / "config" / "channels.json")
    monkeypatch.setattr(relay, "discord_id_to_agent", {A_BOT: "a", B_BOT: "b"})
    relay.load_config()
    return tmp_path


@pytest.fixture
def adapter(relay, workspace):
    class TestAdapter(relay.DiscordAdapter):
        user = Author(A_BOT, bot=True)

    a = TestAdapter.__new__(TestAdapter)
    a.http_session = None
    a.server_ids = {"42"}
    a.reply_gate = relay.ReplyGate()
    a.guest_budget = relay.GuestBudget()
    a.routed = []

    async def fake_capture(m):
        return None

    async def fake_send(m, route):
        a.routed.append(route)

    a.capture_message = fake_capture
    a.send_to_agent_server = fake_send
    return a


def deliver(adapter, msg):
    asyncio.run(adapter.on_message(msg))
    return adapter.routed


def mention_b():
    return [Mention(B_BOT)]


# -- B24 ---------------------------------------------------------------------

def test_unlisted_mention_human_not_routed(adapter, caplog):
    with caplog.at_level(logging.INFO):
        deliver(adapter, Msg(UNLISTED, "<@222> hi", mentions=mention_b()))
    assert adapter.routed == []
    assert f"route channel={UNLISTED} unlisted, mention ignored" in caplog.text
    assert "<@222> hi" not in caplog.text


def test_listed_mention_human_routes_to_b(adapter):
    deliver(adapter, Msg(CH["ops"], "<@222> hi", mentions=mention_b()))
    assert [r.shard for r in adapter.routed] == ["b"]


def test_unlisted_mention_bot_not_routed(adapter):
    deliver(adapter, Msg(UNLISTED, "<@222> hi", author=OTHER_BOT, bot=True,
                         mentions=mention_b()))
    assert adapter.routed == []


def test_listed_mention_bot_routes(adapter, relay):
    # A sibling agent (a's bot, not "us" here) mentioning b in a listed channel.
    sibling = 333
    relay.discord_id_to_agent[sibling] = "m"
    deliver(adapter, Msg(CH["ops"], "<@222> hi", author=sibling, bot=True,
                         mentions=mention_b()))
    assert [r.shard for r in adapter.routed] == ["b"]


# -- payload ------------------------------------------------------------------

@pytest.mark.parametrize("chan,agent,shard", [
    ("ops", "a", "a-2"), ("general", "a", "a"),
    ("b-room", "b", "b"), ("lobby", "a", "a"),
])
def test_routes_per_channel(adapter, chan, agent, shard):
    deliver(adapter, Msg(CH[chan]))
    assert [(r.agent, r.shard) for r in adapter.routed] == [(agent, shard)]


def test_opt_out_drops_lobby(adapter, relay, workspace):
    (workspace / "config" / "channels.json").write_text(
        json.dumps(channels_json(lobby={"route": False})))
    relay.load_config()
    deliver(adapter, Msg(CH["lobby"]))
    assert adapter.routed == []


def test_bot_on_channel_default_not_routed(adapter):
    deliver(adapter, Msg(CH["ops"], author=OTHER_BOT, bot=True))
    assert adapter.routed == []


def test_payload_posts_shard(relay, adapter):
    """send_to_agent_server puts shard next to agent in the POST body."""
    posted = {}

    class Resp:
        status = 202

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Sess:
        def post(self, url, json=None, headers=None):
            posted.update(json)
            return Resp()

    cls = relay.DiscordAdapter
    a = cls.__new__(cls)
    a.http_session = Sess()

    async def noatt(m): return []
    async def noctx(m): return None
    a.download_attachments = noatt
    a.resolve_reply_context = noctx
    msg = Msg(CH["ops"], mentions=mention_b())
    asyncio.run(cls.send_to_agent_server(a, msg, relay.routing.Route("a-2", "a", "channel")))
    assert posted["agent"] == "a" and posted["shard"] == "a-2"
    assert posted["mentions_agent"] is True


# -- unchanged paths ----------------------------------------------------------

def test_sys_command_in_unlisted_channel_still_runs(adapter, relay, monkeypatch):
    seen = []

    async def fake_handle(message, cmd, arg, target, default):
        seen.append(cmd)

    monkeypatch.setattr(adapter, "handle_sys_command", fake_handle, raising=False)
    monkeypatch.setattr(relay, "parse_sys_command", lambda c: ("clear", ""))
    deliver(adapter, Msg(UNLISTED, "<@222> /clear", mentions=mention_b()))
    assert seen == ["clear"] and adapter.routed == []


def test_reply_gate_beats_primary_fallback(adapter, relay, workspace):
    (workspace / "config" / "channels.json").write_text(
        json.dumps(channels_json(lobby={"reply_gate": True})))
    relay.load_config()
    deliver(adapter, Msg(CH["lobby"], "did you feed the cat"))
    assert adapter.routed == []


# -- config freshness ---------------------------------------------------------

def bump(path, content):
    path.write_text(content)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_reload_moves_channel(adapter, relay, workspace, monkeypatch):
    monkeypatch.setattr(relay, "CONFIG_RECHECK_S", 0)
    moved = json.loads(json.dumps(AGENTS))
    moved["agents"]["a"]["shards"] = [{"id": "a", "channels": ["general", "ops"]},
                                      {"id": "a-2", "channels": []}]
    bump(workspace / "config" / "agents.yaml", yaml.safe_dump(moved, sort_keys=False))
    deliver(adapter, Msg(CH["ops"]))
    assert [r.shard for r in adapter.routed] == ["a"]


def test_corrupt_agents_keeps_old_and_logs_once(adapter, relay, workspace, monkeypatch, caplog):
    monkeypatch.setattr(relay, "CONFIG_RECHECK_S", 0)
    bump(workspace / "config" / "agents.yaml", "agents: [unclosed\n  : :")
    with caplog.at_level(logging.ERROR):
        deliver(adapter, Msg(CH["ops"]))
        deliver(adapter, Msg(CH["ops"]))
    assert [r.shard for r in adapter.routed] == ["a-2", "a-2"]
    assert caplog.text.count("keeping previous config") + \
        caplog.text.count("Agent registry unusable") >= 1
    assert caplog.text.count("config reload failed") == 1
