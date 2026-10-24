"""
Tests for the two routing gates in bin/relay.py.

Both exist because the relay used to route a message to an agent on the
strength of the channel's `default_agent` alone, without ever asking who the
message was for:

  #102 — in a channel shared with more than one human, that makes the bot an
         eager third participant in every conversation.
  #103 — for a bot, it makes an unbounded loop. Two installs in one channel
         answer each other until a rate limit or a cost cap intervenes.

The acceptance tests from those issues are pinned here directly: two humans
talking with no mention (silence), and two bots talking (stops after N turns
and says why). The regression that matters most is the last section — an
install that opts into neither gate must behave exactly as it did before.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    """Import bin/relay.py with WORKSPACE_ROOT pointed at a temp tree."""
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()

    import os
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_gate_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_gate_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

AMOS_BOT_ID = 111
KOTHAR_BOT_ID = 222
FOREIGN_BOT_ID = 999
MIKE = 1
ALEX = 2

GATED_CHANNEL = 5000
PLAIN_CHANNEL = 6000
GUEST_CHANNEL = 7000


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, author_id, bot=False, name=None):
        self.id = author_id
        self.bot = bot
        self.display_name = name or f"user-{author_id}"

    def __eq__(self, other):
        # discord.py compares users by snowflake. Without this the fake falls
        # back to identity and `message.author == self.user` is never true, so
        # the self-message check would look tested while never running.
        return getattr(other, "id", object()) == self.id

    def __hash__(self):
        return hash(self.id)


class FakeMention:
    def __init__(self, mention_id, bot=True):
        self.id = mention_id
        self.bot = bot


class FakeRef:
    def __init__(self, author_id):
        self.resolved = type("Resolved", (), {"author": FakeAuthor(author_id)})()


class FakeGuild:
    id = 42


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


_MSG_IDS = __import__("itertools").count(1)


class FakeMessage:
    def __init__(self, author_id, content="", bot=False, channel_id=GATED_CHANNEL,
                 mentions=(), replied_to=None, role_mentions=()):
        self.author = FakeAuthor(author_id, bot=bot)
        self.channel = FakeChannel(channel_id)
        self.content = content
        self.mentions = list(mentions)
        self.role_mentions = list(role_mentions)
        self.reference = FakeRef(replied_to) if replied_to is not None else None
        self.guild = FakeGuild()
        self.id = next(_MSG_IDS)


MENTION_ONLY_CHANNEL = 8000
BOTH_KEYS_CHANNEL = 8100
DUO_CHANNEL = 9000       # amos + guest_agents (2 agents), no reply_gate key -> gated by default
DUO_OFF = 9100           # 2 agents, "reply_gate": false
DUO_MENTION_ONLY = 9200  # 2 agents, mention_only, no reply_gate key
ROLE_ID = 777          # maps to amos
UNMAPPED_ROLE_ID = 778


CHANNELS = {
    "server_id": "42",
    "channels": {
        "kitchen": {"id": str(GATED_CHANNEL), "default_agent": "amos", "reply_gate": True},
        "general": {"id": str(PLAIN_CHANNEL), "default_agent": "amos"},
        "agent-chat": {"id": str(GUEST_CHANNEL), "default_agent": "amos",
                       "guest_agents": True},
        "front-desk": {"id": str(MENTION_ONLY_CHANNEL), "default_agent": "amos",
                       "mention_only": True},
        "both-keys": {"id": str(BOTH_KEYS_CHANNEL), "default_agent": "amos",
                      "reply_gate": True, "mention_only": True},
        "duo": {"id": str(DUO_CHANNEL), "default_agent": "amos", "guest_agents": True},
        "duo-off": {"id": str(DUO_OFF), "default_agent": "amos", "guest_agents": True,
                    "reply_gate": False},
        "duo-mention": {"id": str(DUO_MENTION_ONLY), "default_agent": "amos",
                        "guest_agents": True, "mention_only": True},
    },
}


@pytest.fixture
def adapter(relay, monkeypatch):
    """A DiscordAdapter with no Discord connection, recording what it routes."""
    monkeypatch.setattr(relay, "channels_config", CHANNELS)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    import registry
    monkeypatch.setattr(relay, "registry_obj", registry.parse_registry({
        "version": 2, "agents": {
            "amos": {"name": "amos", "role": "primary", "shards": [
                {"id": "amos", "channels": ["kitchen", "general", "agent-chat",
                                            "front-desk", "both-keys", "duo", "duo-off",
                                            "duo-mention"]}]},
            "kothar": {"name": "kothar", "role": "monitor"}}}))
    monkeypatch.setattr(relay, "discord_id_to_agent",
                        {AMOS_BOT_ID: "amos", KOTHAR_BOT_ID: "kothar"})
    monkeypatch.setattr(relay, "discord_role_id_to_agent", {ROLE_ID: "amos"})

    # discord.Client.user is a read-only property, so the identity of "us" has
    # to be supplied by a subclass rather than assigned.
    class TestAdapter(relay.DiscordAdapter):
        user = FakeAuthor(AMOS_BOT_ID, bot=True)

    a = TestAdapter.__new__(TestAdapter)
    a.http_session = None
    a.server_ids = {"42"}
    a.reply_gate = relay.ReplyGate()
    a.channel_context = relay.channel_context.ChannelContext()
    a.guest_budget = relay.GuestBudget()
    a.routed = []

    async def fake_capture(message):
        return None

    async def fake_send(message, agent):
        a.routed.append((agent.agent, message.content))   # agent is a Route; compare the agent id explicitly

    a.capture_message = fake_capture
    a.send_to_agent_server = fake_send
    return a


def deliver(adapter, message):
    asyncio.run(adapter.on_message(message))
    return adapter.routed


def mention_amos():
    return [FakeMention(AMOS_BOT_ID)]


# ---------------------------------------------------------------------------
# #102 — the reply gate in a shared human channel
# ---------------------------------------------------------------------------

def test_two_humans_talking_are_left_alone(adapter):
    """THE acceptance test: two humans converse with no mention of the bot."""
    deliver(adapter, FakeMessage(MIKE, "did you feed the cat"))
    deliver(adapter, FakeMessage(ALEX, "yeah this morning"))
    assert adapter.routed == []


def test_mention_engages(adapter):
    """...and the other half of it: mention it and it answers."""
    deliver(adapter, FakeMessage(MIKE, "<@111> what's on the calendar",
                                 mentions=mention_amos()))
    assert adapter.routed == [("amos", "<@111> what's on the calendar")]


def test_addressed_by_name_engages(adapter):
    deliver(adapter, FakeMessage(MIKE, "amos, add milk to the list"))
    assert adapter.routed == [("amos", "amos, add milk to the list")]


def test_name_must_lead_the_message(adapter):
    """Talking *about* the bot is not talking *to* it."""
    deliver(adapter, FakeMessage(MIKE, "I think amos already did that"))
    assert adapter.routed == []


def test_reply_to_the_agent_engages(adapter):
    deliver(adapter, FakeMessage(MIKE, "no, the other one", replied_to=AMOS_BOT_ID))
    assert adapter.routed == [("amos", "no, the other one")]


def test_reply_to_another_human_is_the_clearest_not_for_me(adapter):
    deliver(adapter, FakeMessage(MIKE, "can you grab it?", replied_to=ALEX))
    assert adapter.routed == []


def test_actionable_but_unaddressed_stays_silent(relay, adapter):
    """ASK is treated as silence: butting in costs them the conversation."""
    verdict, _ = adapter.reply_gate.decide(
        channel_id=GATED_CHANNEL, content="can you check the oven?",
        mentions_agent=False, replied_to_author_id=None,
        agent_ids={AMOS_BOT_ID}, agent_names=["amos"],
    )
    assert verdict == "ask"
    deliver(adapter, FakeMessage(MIKE, "can you check the oven?"))
    assert adapter.routed == []


# ---------------------------------------------------------------------------
# #102 — volley detection
# ---------------------------------------------------------------------------

def test_volley_silences_an_ambiguous_message(relay, adapter):
    gate = adapter.reply_gate
    now = 1000.0
    for i in range(8):
        gate.decide(channel_id=GATED_CHANNEL, content="chatter", mentions_agent=False,
                    replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + i)
    verdict, reason = gate.decide(
        channel_id=GATED_CHANNEL, content="what about tuesday", mentions_agent=False,
        replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + 9,
    )
    assert verdict == "silent"
    assert "volley" in reason


def test_participation_overrides_the_volley_rule(relay, adapter):
    """Otherwise it goes mute exactly when a conversation it is in gets lively."""
    gate = adapter.reply_gate
    now = 1000.0
    gate.note_agent_post(GATED_CHANNEL, now=now)
    for i in range(8):
        gate.decide(channel_id=GATED_CHANNEL, content="chatter", mentions_agent=False,
                    replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + i)
    verdict, _ = gate.decide(
        channel_id=GATED_CHANNEL, content="what about tuesday", mentions_agent=False,
        replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + 9,
    )
    assert verdict == "ask", "a participant should not be silenced by the volley rule"


def test_a_mention_beats_a_volley(relay, adapter):
    gate = adapter.reply_gate
    now = 1000.0
    for i in range(20):
        gate.decide(channel_id=GATED_CHANNEL, content="chatter", mentions_agent=False,
                    replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + i)
    verdict, _ = gate.decide(
        channel_id=GATED_CHANNEL, content="<@111> stop", mentions_agent=True,
        replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + 21,
    )
    assert verdict == "engage"


# ---------------------------------------------------------------------------
# #103 — bot-to-bot turn cap
# ---------------------------------------------------------------------------

def test_a_bot_never_routes_on_the_channel_default(adapter):
    """The root of the runaway. A human here would reach amos by default; a bot
    must not, or two installs in one channel answer each other forever."""
    deliver(adapter, FakeMessage(FOREIGN_BOT_ID, "interesting point!", bot=True,
                                 channel_id=GUEST_CHANNEL))
    assert adapter.routed == []


def test_foreign_bot_ignored_where_the_channel_has_not_opted_in(adapter):
    deliver(adapter, FakeMessage(FOREIGN_BOT_ID, "<@111> hello", bot=True,
                                 channel_id=PLAIN_CHANNEL, mentions=mention_amos()))
    assert adapter.routed == []


def test_foreign_bot_may_address_an_agent_in_a_guest_channel(adapter):
    deliver(adapter, FakeMessage(FOREIGN_BOT_ID, "<@111> hello", bot=True,
                                 channel_id=GUEST_CHANNEL, mentions=mention_amos()))
    assert adapter.routed == [("amos", "<@111> hello")]


def test_bot_exchange_stops_at_the_limit_and_says_why(adapter):
    """THE acceptance test for #103: two installs talking, capped, and visible."""
    limit = adapter.guest_budget.limit
    posted = []
    for i in range(limit + 4):
        msg = FakeMessage(FOREIGN_BOT_ID, f"<@111> turn {i}", bot=True,
                          channel_id=GUEST_CHANNEL, mentions=mention_amos())
        deliver(adapter, msg)
        posted.extend(msg.channel.sent)

    assert len(adapter.routed) == limit, "the exchange must stop at the limit"
    # ...and stopping silently is indistinguishable from being broken.
    notices = [t for t in posted if "GUEST_TURN_LIMIT" in t]
    assert len(notices) == 1, "say why exactly once — the notice must not become the spam"


def test_the_stop_notice_is_posted_once_per_exhaustion(adapter):
    budget = adapter.guest_budget
    for _ in range(budget.limit):
        assert budget.take(GUEST_CHANNEL)[0] is True
    assert budget.take(GUEST_CHANNEL) == (False, budget.limit + 1, True)
    assert budget.take(GUEST_CHANNEL)[2] is False, "second refusal must stay quiet"


def test_a_human_speaking_refills_the_budget(adapter):
    limit = adapter.guest_budget.limit
    for i in range(limit + 2):
        deliver(adapter, FakeMessage(FOREIGN_BOT_ID, f"<@111> turn {i}", bot=True,
                                     channel_id=GUEST_CHANNEL, mentions=mention_amos()))
    assert len(adapter.routed) == limit

    deliver(adapter, FakeMessage(MIKE, "ok you two", channel_id=GUEST_CHANNEL))
    deliver(adapter, FakeMessage(FOREIGN_BOT_ID, "<@111> back", bot=True,
                                 channel_id=GUEST_CHANNEL, mentions=mention_amos()))
    assert adapter.routed[-1] == ("amos", "<@111> back")


def test_sibling_agents_are_capped_too(adapter):
    """Two of our own agents in one channel is the same loop with a nicer name."""
    limit = adapter.guest_budget.limit
    for i in range(limit + 3):
        deliver(adapter, FakeMessage(KOTHAR_BOT_ID, f"<@111> turn {i}", bot=True,
                                     channel_id=PLAIN_CHANNEL, mentions=mention_amos()))
    assert len(adapter.routed) == limit


def test_the_budget_is_per_channel(adapter):
    budget = adapter.guest_budget
    for _ in range(budget.limit):
        budget.take(GUEST_CHANNEL)
    assert budget.take(GUEST_CHANNEL)[0] is False
    assert budget.take(PLAIN_CHANNEL)[0] is True


# ---------------------------------------------------------------------------
# Regressions — an install that opts into neither gate is untouched
# ---------------------------------------------------------------------------

def test_ungated_channel_still_routes_everything(adapter):
    """#general has no reply_gate: humans reach the default agent as before."""
    deliver(adapter, FakeMessage(MIKE, "no mention, no reply, nothing",
                                 channel_id=PLAIN_CHANNEL))
    assert adapter.routed == [("amos", "no mention, no reply, nothing")]


def test_sys_commands_are_not_swallowed_by_the_gate(adapter, relay, monkeypatch):
    """A wedged agent is exactly when you cannot afford a gate to eat /clear."""
    handled = []

    async def fake_handle(message, cmd, args, mentioned, channel_default):
        handled.append((cmd, channel_default))

    adapter.handle_sys_command = fake_handle
    deliver(adapter, FakeMessage(MIKE, "/clear", channel_id=GATED_CHANNEL))

    assert handled == [("clear", "amos")], "an owner's /clear must survive the gate"
    assert adapter.routed == []


def test_our_own_posts_are_still_ignored(adapter):
    """The mention is what makes this bite. Without it the bot branch below
    would refuse the message anyway (no target agent), and the test would pass
    with the self-check deleted."""
    deliver(adapter, FakeMessage(AMOS_BOT_ID, "<@111> something I said", bot=True,
                                 mentions=mention_amos()))
    assert adapter.routed == []


def test_our_own_post_counts_as_participation(adapter):
    """Our own traffic is the only evidence that we are in a conversation, and
    it is discarded a line later — so it has to be recorded on the way past."""
    deliver(adapter, FakeMessage(AMOS_BOT_ID, "on it", bot=True,
                                 channel_id=GATED_CHANNEL))
    now = adapter.reply_gate._state(GATED_CHANNEL)["last_post"]
    assert now > 0, "the gate never learned we spoke"

    for i in range(8):
        adapter.reply_gate.decide(
            channel_id=GATED_CHANNEL, content="chatter", mentions_agent=False,
            replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + i)
    verdict, _ = adapter.reply_gate.decide(
        channel_id=GATED_CHANNEL, content="and tuesday?", mentions_agent=False,
        replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + 9)
    assert verdict == "ask", "we just spoke here; the volley rule should not apply"


def test_messages_from_other_servers_are_still_ignored(adapter):
    msg = FakeMessage(MIKE, "<@111> hello", mentions=mention_amos())
    msg.guild = type("OtherGuild", (), {"id": 777})()
    deliver(adapter, msg)
    assert adapter.routed == []


# ---------------------------------------------------------------------------
# mention_only channels — answer a direct or role mention, nothing else
# ---------------------------------------------------------------------------

def test_mention_only_plain_message_is_silent_even_with_a_shard_owning_it(adapter):
    """amos owns front-desk through its shard; ownership alone does not route."""
    deliver(adapter, FakeMessage(MIKE, "what's for dinner", channel_id=MENTION_ONLY_CHANNEL))
    assert adapter.routed == []


def test_mention_only_reply_to_the_agent_is_silent(adapter):
    deliver(adapter, FakeMessage(MIKE, "no, the other one", channel_id=MENTION_ONLY_CHANNEL,
                                 replied_to=AMOS_BOT_ID))
    assert adapter.routed == []


def test_mention_only_name_opener_is_silent(adapter):
    deliver(adapter, FakeMessage(MIKE, "amos, add milk", channel_id=MENTION_ONLY_CHANNEL))
    assert adapter.routed == []


def test_mention_only_user_mention_routes(adapter):
    deliver(adapter, FakeMessage(MIKE, "<@111> add milk", channel_id=MENTION_ONLY_CHANNEL,
                                 mentions=mention_amos()))
    assert adapter.routed == [("amos", "<@111> add milk")]


def test_mention_only_role_mention_routes(adapter):
    deliver(adapter, FakeMessage(MIKE, "<@&777> add milk", channel_id=MENTION_ONLY_CHANNEL,
                                 role_mentions=[FakeRole(ROLE_ID)]))
    assert adapter.routed == [("amos", "<@&777> add milk")]


def test_mention_only_unmapped_role_mention_is_silent(adapter):
    deliver(adapter, FakeMessage(MIKE, "<@&778> add milk", channel_id=MENTION_ONLY_CHANNEL,
                                 role_mentions=[FakeRole(UNMAPPED_ROLE_ID)]))
    assert adapter.routed == []


def test_mention_only_beats_reply_gate_when_both_are_set(adapter):
    """A channel that sets both keys is mention_only: no gate, no name-opener."""
    deliver(adapter, FakeMessage(MIKE, "amos, add milk", channel_id=BOTH_KEYS_CHANNEL))
    deliver(adapter, FakeMessage(MIKE, "anyone want lunch", channel_id=BOTH_KEYS_CHANNEL))
    assert adapter.routed == []
    deliver(adapter, FakeMessage(MIKE, "<@111> lunch?", channel_id=BOTH_KEYS_CHANNEL,
                                 mentions=mention_amos()))
    assert adapter.routed == [("amos", "<@111> lunch?")]


def test_load_config_logs_the_conflict_and_keeps_going(relay, monkeypatch, tmp_path):
    """Both keys on one channel: an error is logged at load, nothing crashes."""
    import json
    import registry

    class Recorder:
        def __init__(self):
            self.errors = []

        def error(self, msg, *a, **k):
            self.errors.append(msg)

        def __getattr__(self, name):
            return lambda *a, **k: None

    chan_file = tmp_path / "channels.json"
    chan_file.write_text(json.dumps(CHANNELS))
    recorder = Recorder()
    monkeypatch.setattr(relay, "log", recorder)
    monkeypatch.setattr(relay, "CHANNELS_CONFIG_PATH", chan_file)
    monkeypatch.setattr(relay.agent_registry, "load_registry", lambda ws: registry.parse_registry({
        "version": 2, "agents": {
            "amos": {"name": "amos", "role": "primary", "shards": [
                {"id": "amos", "channels": ["both-keys"]}]},
            "kothar": {"name": "kothar", "role": "monitor"}}}))
    monkeypatch.setattr(relay, "discord_id_to_agent", {})
    monkeypatch.setattr(relay, "discord_role_id_to_agent", {})

    relay.load_config()

    assert any("both-keys" in e and "mention_only" in e for e in recorder.errors)
    assert not any("front-desk" in e for e in recorder.errors)


def test_role_id_env_is_read_from_the_registry_into_the_relay_map(relay, monkeypatch, tmp_path):
    import registry
    monkeypatch.setenv("DISCORD_ROLE_ID_AMOS", str(ROLE_ID))
    monkeypatch.setattr(relay.agent_registry, "load_registry", lambda ws: registry.parse_registry({
        "version": 2, "agents": {
            "amos": {"name": "amos", "role": "primary",
                     "discord": {"role_id_env": "DISCORD_ROLE_ID_AMOS"},
                     "shards": [{"id": "amos", "channels": []}]},
            "kothar": {"name": "kothar", "role": "monitor"}}}))
    monkeypatch.setattr(relay, "discord_id_to_agent", {})
    monkeypatch.setattr(relay, "discord_role_id_to_agent", {})
    monkeypatch.setattr(relay, "CHANNELS_CONFIG_PATH", tmp_path / "channels.json")

    relay.load_config()

    assert relay.discord_role_id_to_agent == {ROLE_ID: "amos"}


# ---------------------------------------------------------------------------
# #131 — the gate is on by default for channels with two or more agents
# ---------------------------------------------------------------------------

def test_two_agent_channel_is_gated_by_default(adapter):
    deliver(adapter, FakeMessage(MIKE, "did you feed the cat", channel_id=DUO_CHANNEL))
    deliver(adapter, FakeMessage(ALEX, "yeah this morning", channel_id=DUO_CHANNEL))
    assert adapter.routed == []


def test_two_agent_channel_mention_still_routes(adapter):
    deliver(adapter, FakeMessage(MIKE, "<@111> what's on the calendar",
                                 channel_id=DUO_CHANNEL, mentions=mention_amos()))
    assert adapter.routed == [("amos", "<@111> what's on the calendar")]


def test_explicit_false_opts_out(adapter):
    deliver(adapter, FakeMessage(MIKE, "did you feed the cat", channel_id=DUO_OFF))
    assert adapter.routed == [("amos", "did you feed the cat")]


def test_single_agent_channel_not_gated_by_default(adapter):
    deliver(adapter, FakeMessage(MIKE, "did you feed the cat", channel_id=PLAIN_CHANNEL))
    assert adapter.routed == [("amos", "did you feed the cat")]


def test_guest_agents_channel_counts_the_guest(relay, adapter):
    cfg = CHANNELS["channels"]["agent-chat"]
    assert relay.channel_agent_count("agent-chat", cfg) == 2
    assert relay.channel_agent_count("general", CHANNELS["channels"]["general"]) == 1
    assert relay.channel_agent_count(None, cfg) == 0


def test_env_switch_restores_opt_in(adapter, monkeypatch):
    monkeypatch.setenv("KARAKOS_REPLY_GATE_DEFAULT", "off")
    deliver(adapter, FakeMessage(MIKE, "did you feed the cat", channel_id=DUO_CHANNEL))
    assert adapter.routed == [("amos", "did you feed the cat")]


def test_env_switch_leaves_explicit_true_alone(adapter, monkeypatch):
    monkeypatch.setenv("KARAKOS_REPLY_GATE_DEFAULT", "off")
    deliver(adapter, FakeMessage(MIKE, "did you feed the cat"))   # kitchen: reply_gate true
    assert adapter.routed == []


def test_possessive_name_is_not_a_trigger(adapter):
    deliver(adapter, FakeMessage(MIKE, "amos's reply is slow", channel_id=DUO_CHANNEL))
    deliver(adapter, FakeMessage(MIKE, "amos is investigating", channel_id=DUO_CHANNEL))
    deliver(adapter, FakeMessage(MIKE, "amos already did that", channel_id=DUO_CHANNEL))
    assert adapter.routed == []


def test_vocative_name_engages(adapter):
    deliver(adapter, FakeMessage(MIKE, "hey amos, add milk", channel_id=DUO_CHANNEL))
    deliver(adapter, FakeMessage(MIKE, "amos", channel_id=DUO_CHANNEL))
    assert adapter.routed == [("amos", "hey amos, add milk"), ("amos", "amos")]


def test_mention_only_beats_default_gate(adapter, relay, monkeypatch):
    errors = []

    class Recorder:
        def error(self, msg, *a, **k):
            errors.append(msg % a if a else msg)

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(relay, "log", Recorder())
    deliver(adapter, FakeMessage(MIKE, "amos, add milk", channel_id=DUO_MENTION_ONLY))
    deliver(adapter, FakeMessage(MIKE, "anyone want lunch", channel_id=DUO_MENTION_ONLY))
    assert adapter.routed == []
    deliver(adapter, FakeMessage(MIKE, "<@111> lunch?", channel_id=DUO_MENTION_ONLY,
                                 mentions=mention_amos()))
    assert adapter.routed == [("amos", "<@111> lunch?")]
    assert errors == []
