"""Relay side of the optional Discord behaviours (step 6.2): thread routing,
reaction notices and edit reroute, with stub Discord objects. No gateway, no
HTTP: the adapter records what it would send to the agent server."""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")

A_BOT, B_BOT, MIKE, SAM = 111, 222, 1, 2
GENERAL, OPS, UNLISTED = 1001, 1002, 4040
THREAD_UNDER_GENERAL, THREAD_UNDER_UNLISTED = 5001, 5002

AGENTS = {"version": 2, "agents": {
    "a": {"name": "a", "role": "primary", "shards": [
        {"id": "a", "channels": ["general"]}, {"id": "a-2", "channels": ["ops"]}]},
    "b": {"name": "b", "role": "custom", "shards": [{"id": "b", "channels": []}]},
    "m": {"name": "m", "role": "monitor"}}}


def channels(ux=None, top=None):
    cfg = {"server_id": "42", "channels": {
        "general": {"id": str(GENERAL), **({"ux": ux} if ux is not None else {})},
        "ops": {"id": str(OPS)}}}
    if top is not None:
        cfg["ux"] = top
    return cfg


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_ux_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_ux_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


class Author:
    def __init__(self, i, bot=False, name=None):
        self.id, self.bot, self.display_name = i, bot, name or f"u{i}"

    def __eq__(self, o):
        return getattr(o, "id", object()) == self.id

    def __hash__(self):
        return hash(self.id)


class Chan:
    def __init__(self, cid, parent_id=None, messages=None):
        self.id, self.parent_id = cid, parent_id
        self.guild = SimpleNamespace(id=42, name="Test Guild")
        self.messages = messages or {}
        self.fetches = 0

    async def fetch_message(self, mid):
        self.fetches += 1
        if mid not in self.messages:
            raise RuntimeError("unknown message")
        return self.messages[mid]


class Msg:
    def __init__(self, channel, content="hello", author=None, mid=5555, mentions=()):
        self.author = author or Author(MIKE)
        self.channel, self.content, self.id = channel, content, mid
        self.mentions, self.reference, self.guild = list(mentions), None, SimpleNamespace(id=42, name="Test Guild")
        self.attachments = []


class Http:
    def __init__(self, status=202):
        self.posts, self.status = [], status

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        status = self.status

        class Ctx:
            async def __aenter__(s):
                return SimpleNamespace(status=status, text=lambda: _text())

            async def __aexit__(s, *a):
                return False
        return Ctx()


async def _text():
    return ""


@pytest.fixture
def setup(relay, tmp_path, monkeypatch):
    def make(cfg, owner=MIKE):
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "agents.yaml").write_text(yaml.safe_dump(AGENTS, sort_keys=False))
        (tmp_path / "config" / "channels.json").write_text(json.dumps(cfg))
        monkeypatch.setattr(relay, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(relay, "AGENTS_CONFIG_PATH", tmp_path / "config" / "agents.yaml")
        monkeypatch.setattr(relay, "CHANNELS_CONFIG_PATH", tmp_path / "config" / "channels.json")
        monkeypatch.setattr(relay, "discord_id_to_agent", {A_BOT: "a", B_BOT: "b"})
        monkeypatch.setattr(relay, "OWNER_DISCORD_ID", owner)
        relay.load_config()

        chans = {}

        class TestAdapter(relay.DiscordAdapter):
            user = Author(A_BOT, bot=True)

            def get_channel(self, cid):
                return chans.get(cid)

        a = TestAdapter.__new__(TestAdapter)
        a.http_session = Http()
        a.server_ids = {"42"}
        a.reply_gate = relay.ReplyGate()
        a.guest_budget = relay.GuestBudget()
        a.edits = []

        async def fake_capture(m):
            return None

        async def fake_post_json(path, payload=None):
            a.edits.append((path, payload))
            return True, "", {"status": "updated"}

        a.capture_message = fake_capture
        a.agent_server_post_json = fake_post_json
        a.chans = chans
        return a
    return make


def run(coro):
    return asyncio.run(coro)


# -- thread routing --------------------------------------------------------------

def test_thread_under_listed_parent_routes_to_parent_owner(relay, setup):
    a = setup(channels())
    thread = Chan(THREAD_UNDER_GENERAL, parent_id=GENERAL)
    run(a.on_message(Msg(thread)))
    (url, body), = a.http_session.posts
    assert url.endswith("/message")
    assert body["shard"] == "a" and body["channel_id"] == str(THREAD_UNDER_GENERAL)
    assert body["channel"] == "general" and body["thread_parent_id"] == str(GENERAL)


def test_thread_under_unlisted_parent_is_refused(relay, setup):
    a = setup(channels())
    run(a.on_message(Msg(Chan(THREAD_UNDER_UNLISTED, parent_id=UNLISTED),
                         mentions=[SimpleNamespace(id=A_BOT, bot=True)])))
    assert a.http_session.posts == []


def test_ordinary_channel_is_unchanged(relay, setup):
    a = setup(channels())
    run(a.on_message(Msg(Chan(OPS))))
    (_, body), = a.http_session.posts
    assert body["shard"] == "a-2" and body["channel_id"] == str(OPS) \
        and body["channel"] == "ops" and "thread_parent_id" not in body


# -- reactions -------------------------------------------------------------------

def reaction(user=MIKE, mid=9000, channel=GENERAL, emoji="👍", member=None):
    return SimpleNamespace(guild_id=42, user_id=user, channel_id=channel, message_id=mid,
                           emoji=emoji, member=member or Author(user, name=f"u{user}"))


def agent_chan(cid=GENERAL, parent_id=None, mid=9000, author=A_BOT, content="done: deployed\nok"):
    return Chan(cid, parent_id, {mid: Msg(None, content, Author(author, bot=True), mid)})


ON = {"reaction_notices": "owner"}


def test_owner_reaction_sends_exact_notice(relay, setup):
    a = setup(channels(ux=ON))
    a.chans[GENERAL] = agent_chan()
    run(a.on_raw_reaction_add(reaction()))
    (url, body), = a.http_session.posts
    assert url.endswith("/message")
    assert body["content"] == ('[reaction, no reply needed unless it changes something] '
                               'u1 reacted 👍 to your message: "done: deployed ok"')
    assert body["message_id"].startswith("reaction:9000:1:") and len(body["message_id"].split(":")[3]) == 8
    assert (body["agent"], body["shard"], body["is_bot"], body["mentions_agent"]) == ("a", "a", False, False)
    assert body["channel_id"] == str(GENERAL) and body["author"] == "u1"
    route = relay.routing.route_message(relay.registry_obj, "general", "a", False)
    assert body["shard"] == route.shard


def test_repeat_within_cooldown_sends_nothing(relay, setup):
    a = setup(channels(ux=ON))
    a.chans[GENERAL] = agent_chan()
    run(a.on_raw_reaction_add(reaction()))
    run(a.on_raw_reaction_add(reaction()))
    assert len(a.http_session.posts) == 1
    # a different emoji is a different message_id but the same (user, message): cooled down
    run(a.on_raw_reaction_add(reaction(emoji="✅")))
    assert len(a.http_session.posts) == 1


def test_non_owner_dropped_under_owner_sent_under_humans(relay, setup):
    a = setup(channels(ux=ON))
    a.chans[GENERAL] = agent_chan()
    run(a.on_raw_reaction_add(reaction(user=SAM)))
    assert a.http_session.posts == []
    b = setup(channels(ux={"reaction_notices": "humans"}))
    b.chans[GENERAL] = agent_chan()
    run(b.on_raw_reaction_add(reaction(user=SAM)))
    assert len(b.http_session.posts) == 1


@pytest.mark.parametrize("case", ["bot_reactor", "human_message", "unlisted", "fetch_fails",
                                  "switch_off", "agent_reactor"])
def test_dropped_reactions(relay, setup, case):
    a = setup(channels(ux=ON if case != "switch_off" else {"reaction_notices": False}))
    a.chans[GENERAL] = agent_chan(author=SAM if case == "human_message" else A_BOT)
    a.chans[UNLISTED] = agent_chan(cid=UNLISTED)
    if case == "fetch_fails":
        a.chans[GENERAL] = Chan(GENERAL)
    ev = reaction()
    if case == "bot_reactor":
        ev = reaction(member=Author(MIKE, bot=True))
    if case == "unlisted":
        ev = reaction(channel=UNLISTED)
    if case == "agent_reactor":
        ev = reaction(user=B_BOT)
    run(a.on_raw_reaction_add(ev))
    assert a.http_session.posts == []


def test_reaction_in_thread_resolves_parent(relay, setup):
    a = setup(channels(ux=ON))
    a.chans[THREAD_UNDER_GENERAL] = agent_chan(cid=THREAD_UNDER_GENERAL, parent_id=GENERAL)
    run(a.on_raw_reaction_add(reaction(channel=THREAD_UNDER_GENERAL)))
    (_, body), = a.http_session.posts
    assert body["channel_id"] == str(THREAD_UNDER_GENERAL) and body["thread_parent_id"] == str(GENERAL)


def test_eleventh_notice_in_a_minute_is_dropped(relay, setup):
    a = setup(channels(ux=ON))
    chan = Chan(GENERAL, None, {9000 + i: Msg(None, "x", Author(A_BOT, bot=True), 9000 + i)
                                for i in range(12)})
    a.chans[GENERAL] = chan
    for i in range(12):
        run(a.on_raw_reaction_add(reaction(mid=9000 + i)))
    assert len(a.http_session.posts) == relay.REACTION_NOTICE_RATE == 10


def test_rate_window_slides(relay, setup, monkeypatch):
    a = setup(channels(ux=ON))
    a.chans[GENERAL] = Chan(GENERAL, None, {9000 + i: Msg(None, "x", Author(A_BOT, bot=True), 9000 + i)
                                            for i in range(12)})
    now = {"t": 100.0}
    monkeypatch.setattr(relay, "time", SimpleNamespace(monotonic=lambda: now["t"], time=lambda: now["t"]))
    for i in range(11):
        run(a.on_raw_reaction_add(reaction(mid=9000 + i)))
    now["t"] += 61
    run(a.on_raw_reaction_add(reaction(mid=9011)))
    assert len(a.http_session.posts) == 11


# -- edits -----------------------------------------------------------------------

def edit(data=None, mid=7000, channel=GENERAL, cached=None):
    return SimpleNamespace(message_id=mid, channel_id=channel, guild_id=42,
                           data=data if data is not None else {"content": "fixed", "author": {"id": str(MIKE)}},
                           cached_message=cached)


EDIT_ON = {"edit_reroute": True}


def test_edit_posts_to_edit_route_with_fetched_content(relay, setup):
    a = setup(channels(ux=EDIT_ON))
    a.chans[GENERAL] = Chan(GENERAL, None, {7000: Msg(None, "fixed text", Author(MIKE), 7000)})
    run(a.on_raw_message_edit(edit(cached=SimpleNamespace(content="old text"))))
    assert a.edits == [("/message/edit", {
        "server": "Test Guild", "message_id": "7000", "channel_id": str(GENERAL),
        "author_id": str(MIKE), "content": "fixed text", "before": "old text"})]


def test_edit_falls_back_to_raw_text_when_fetch_fails(relay, setup):
    a = setup(channels(ux=EDIT_ON))
    a.chans[GENERAL] = Chan(GENERAL)
    run(a.on_raw_message_edit(edit()))
    (path, body), = a.edits
    assert body["content"] == "fixed" and body["author_id"] == str(MIKE) and body["before"] is None


@pytest.mark.parametrize("case", ["embed_only", "bot_author", "unlisted", "switch_off", "bad_guild"])
def test_dropped_edits(relay, setup, case):
    a = setup(channels(ux=EDIT_ON if case != "switch_off" else {"edit_reroute": False}))
    a.chans[GENERAL] = Chan(GENERAL, None, {7000: Msg(None, "x", Author(MIKE), 7000)})
    a.chans[UNLISTED] = Chan(UNLISTED)
    ev = edit()
    if case == "embed_only":
        ev = edit({"embeds": [{}]})
    if case == "bot_author":
        ev = edit({"content": "x", "author": {"id": "9", "bot": True}})
    if case == "unlisted":
        ev = edit(channel=UNLISTED)
    if case == "bad_guild":
        ev.guild_id = 99
    run(a.on_raw_message_edit(ev))
    assert a.edits == []


# -- everything off -----------------------------------------------------------------

def test_all_switches_off_events_do_nothing(relay, setup):
    a = setup(channels())
    a.chans[GENERAL] = agent_chan()
    run(a.on_raw_reaction_add(reaction()))
    run(a.on_raw_message_edit(edit()))
    assert a.http_session.posts == [] and a.edits == [] and a.chans[GENERAL].fetches == 0


def test_handlers_defined_once():
    src = RELAY_PATH.read_text()
    assert src.count("def on_raw_reaction_add") == 1 and src.count("def on_raw_message_edit") == 1
