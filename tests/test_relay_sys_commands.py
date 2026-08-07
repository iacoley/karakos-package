"""
Tests for the relay-side system commands in bin/relay.py.

These run in the relay process rather than the agent, because the case they
exist for is an agent too wedged to read its own queue. That makes three
things load-bearing and each is pinned here: the owner gate, the refusal to
guess which agent a command acts on, and the fact that an ordinary message
beginning with a slash still reaches the agent.
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
        spec = importlib.util.spec_from_file_location("relay_sys_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_sys_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_bare_command_parses(relay):
    assert relay.parse_sys_command("/clear") == ("clear", "")


def test_sys_prefix_still_parses(relay):
    """The retired `/sys ` prefix is still in people's fingers."""
    assert relay.parse_sys_command("/sys clear") == ("clear", "")


def test_mentions_are_stripped_before_matching(relay):
    """`@Agent /sys clear` is how you'd naturally address one of several bots."""
    assert relay.parse_sys_command("<@123456> /sys clear") == ("clear", "")
    assert relay.parse_sys_command("<@!123456> /clear") == ("clear", "")
    assert relay.parse_sys_command("<@&99> /reload") == ("reload", "")


def test_command_matching_is_case_insensitive(relay):
    assert relay.parse_sys_command("/CLEAR") == ("clear", "")
    assert relay.parse_sys_command("/Sys Reload") == ("reload", "")


def test_arguments_are_carried_through(relay):
    assert relay.parse_sys_command("/reload  extra args ") == ("reload", "extra args")


def test_unknown_slash_word_is_not_a_command(relay):
    """Falls through to the agent — the relay does not own every slash."""
    assert relay.parse_sys_command("/deploy") is None
    assert relay.parse_sys_command("/sys deploy") is None


def test_plain_sentence_is_not_a_command(relay):
    assert relay.parse_sys_command("can you clear the cache") is None
    assert relay.parse_sys_command("") is None


def test_slash_must_lead(relay):
    """A quoted command inside a real sentence is a sentence."""
    assert relay.parse_sys_command('please run "/clear" when you get a chance') is None


def test_bare_sys_with_no_subcommand_is_not_a_command(relay):
    assert relay.parse_sys_command("/sys") is None
    assert relay.parse_sys_command("/sys   ") is None


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def test_mention_wins_over_channel_default(relay):
    agent, err = relay.resolve_target_agent("kothar", "amos", ["amos", "kothar"])
    assert (agent, err) == ("kothar", None)


def test_channel_default_used_when_nothing_mentioned(relay):
    agent, err = relay.resolve_target_agent(None, "amos", ["amos", "kothar"])
    assert (agent, err) == ("amos", None)


def test_sole_agent_needs_no_disambiguation(relay):
    agent, err = relay.resolve_target_agent(None, None, ["amos"])
    assert (agent, err) == ("amos", None)


def test_never_blind_defaults_with_several_agents(relay):
    """The whole point. Guessing clears the wrong agent's session silently."""
    agent, err = relay.resolve_target_agent(None, None, ["amos", "kothar", "herald"])
    assert agent is None
    assert "Which agent?" in err
    # The error has to name the choices, or it is a dead end.
    for name in ("amos", "kothar", "herald"):
        assert f"`{name}`" in err


def test_unknown_mentioned_agent_is_rejected(relay):
    agent, err = relay.resolve_target_agent("nobody", "amos", ["amos"])
    assert agent is None
    assert "nobody" in err


def test_stale_channel_default_is_rejected(relay):
    agent, err = relay.resolve_target_agent(None, "ninkasi", ["amos"])
    assert agent is None
    assert "ninkasi" in err


def test_no_agents_configured(relay):
    agent, err = relay.resolve_target_agent(None, None, [])
    assert agent is None
    assert "No agents" in err


# ---------------------------------------------------------------------------
# Handler — permission gate and dispatch
# ---------------------------------------------------------------------------

class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, author_id):
        self.id = author_id
        self.display_name = f"user-{author_id}"


class FakeMessage:
    def __init__(self, author_id, content=""):
        self.author = FakeAuthor(author_id)
        self.channel = FakeChannel()
        self.content = content


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeHttpSession:
    """Records calls; hands back whatever response is queued for the path."""

    def __init__(self, responses=None):
        self.posts = []
        self.gets = []
        self.responses = responses or {}

    def post(self, url, **kwargs):
        self.posts.append(url)
        return self.responses.get("post", FakeResponse(200))

    def get(self, url, **kwargs):
        self.gets.append(url)
        return self.responses.get("get", FakeResponse(200, {"agents": {}}))


def make_adapter(relay, http_session):
    """A DiscordAdapter with no Discord connection behind it."""
    adapter = relay.DiscordAdapter.__new__(relay.DiscordAdapter)
    adapter.http_session = http_session
    return adapter


OWNER = 4242


def test_non_owner_is_denied_visibly(relay, monkeypatch):
    """A silent denial reads as a broken command, so it has to say so."""
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=999, content="/clear")

    asyncio.run(adapter.handle_sys_command(msg, "clear", "", None, "amos"))

    assert http.posts == [], "a denied command must not reach the agent server"
    assert len(msg.channel.sent) == 1
    assert "Permission denied" in msg.channel.sent[0]


def test_unset_owner_denies_everyone(relay, monkeypatch):
    """Unconfigured must not mean unrestricted — a fresh install that skipped
    OWNER_DISCORD_ID would otherwise hand session control to the whole server."""
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", 0)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=OWNER, content="/clear")

    asyncio.run(adapter.handle_sys_command(msg, "clear", "", None, "amos"))

    assert http.posts == []
    assert "Permission denied" in msg.channel.sent[0]
    assert "OWNER_DISCORD_ID" in msg.channel.sent[0]


def test_owner_clear_hits_the_reset_endpoint(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=OWNER, content="/clear")

    asyncio.run(adapter.handle_sys_command(msg, "clear", "", "kothar", "amos"))

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/kothar/reset"]
    assert "cleared" in msg.channel.sent[0]


def test_owner_reload_hits_the_reload_endpoint(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=OWNER, content="/reload")

    asyncio.run(adapter.handle_sys_command(msg, "reload", "", None, "amos"))

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/amos/reload"]
    assert "reloaded" in msg.channel.sent[0]


def test_ambiguous_target_reaches_no_endpoint(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=OWNER, content="/clear")

    asyncio.run(adapter.handle_sys_command(msg, "clear", "", None, None))

    assert http.posts == []
    assert "Which agent?" in msg.channel.sent[0]


def test_agent_server_failure_is_reported_not_swallowed(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}})
    http = FakeHttpSession({"post": FakeResponse(404, text="Unknown agent")})
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=OWNER, content="/clear")

    asyncio.run(adapter.handle_sys_command(msg, "clear", "", None, "amos"))

    assert "failed" in msg.channel.sent[0]
    assert "404" in msg.channel.sent[0]


def test_status_needs_no_target_agent(relay, monkeypatch):
    """status reports on every agent, so ambiguity does not apply to it."""
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    payload = {"agents": {
        "amos": {"state": "IDLE", "alive": True, "queue_depth": 0},
        "kothar": {"state": "BUSY", "alive": False, "queue_depth": 3},
    }}
    http = FakeHttpSession({"get": FakeResponse(200, payload)})
    adapter = make_adapter(relay, http)
    msg = FakeMessage(author_id=OWNER, content="/status")

    asyncio.run(adapter.handle_sys_command(msg, "status", "", None, None))

    assert http.gets == [f"{relay.AGENT_SERVER_URL}/health"]
    out = msg.channel.sent[0]
    assert "amos" in out and "IDLE" in out and "alive" in out
    assert "kothar" in out and "BUSY" in out and "not running" in out
    assert "queue 3" in out


def test_status_reports_an_unreachable_server(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}})

    class Boom(FakeHttpSession):
        def get(self, url, **kwargs):
            raise ConnectionRefusedError("nope")

    adapter = make_adapter(relay, Boom())
    msg = FakeMessage(author_id=OWNER, content="/status")

    asyncio.run(adapter.handle_sys_command(msg, "status", "", None, None))

    assert "could not reach" in msg.channel.sent[0]
