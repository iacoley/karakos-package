"""
Tests for load_server_ids() in bin/relay.py — which Discord servers the relay
accepts messages from.

The single-server `server_id` written by setup.sh has to keep working
untouched; `server_ids` is the additive path for installs that also join a
shared server.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    """Import bin/relay.py with WORKSPACE_ROOT pointed at a temp tree.

    Import-time side effects (the rotating log handler) need logs/ to exist.
    """
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()

    import os
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


def test_single_server_id_still_works(relay):
    assert relay.load_server_ids({"server_id": "111"}) == {"111"}


def test_server_ids_list_is_accepted(relay):
    assert relay.load_server_ids({"server_ids": ["222", "333"]}) == {"222", "333"}


def test_both_keys_are_unioned(relay):
    got = relay.load_server_ids({"server_id": "111", "server_ids": ["222"]})
    assert got == {"111", "222"}


def test_ints_are_normalized_to_strings(relay):
    """message.guild.id is compared as a string, so the config must normalize."""
    got = relay.load_server_ids({"server_id": 111, "server_ids": [222]})
    assert got == {"111", "222"}


def test_bare_string_server_ids_is_treated_as_one_id(relay):
    """A hand-edited config is as likely to write a string as a list."""
    assert relay.load_server_ids({"server_ids": "222"}) == {"222"}


def test_empty_config_accepts_nothing(relay):
    """No servers configured must not mean 'accept every server'."""
    assert relay.load_server_ids({}) == set()
    assert relay.load_server_ids({"server_id": "", "server_ids": []}) == set()


# -- send_to_agent_server: where a message was posted --------------------------


class _Resp:
    status = 202

    async def text(self):
        return ""


class _Post:
    def __init__(self, sent):
        self._sent = sent

    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, headers=None):
        self.sent.append(json)
        return _Post(self.sent)


async def _send(relay, channel, guild, config_channels):
    """Run DiscordAdapter.send_to_agent_server on a bare instance and return
    the payload it posted to the agent server."""
    adapter = object.__new__(relay.DiscordAdapter)
    adapter.http_session = _Session()

    async def no_attachments(message):
        return []

    async def no_reply(message):
        return None

    adapter.download_attachments = no_attachments
    adapter.resolve_reply_context = no_reply
    message = SimpleNamespace(
        id=999, content="hello", mentions=[], channel=channel, guild=guild,
        author=SimpleNamespace(id=7, display_name="sam", bot=False))
    saved = relay.channels_config
    relay.channels_config = {"channels": config_channels}
    try:
        await adapter.send_to_agent_server(message, SimpleNamespace(agent="a", shard="a"))
    finally:
        relay.channels_config = saved
    (payload,) = adapter.http_session.sent
    return payload


def test_unlisted_channel_yields_discord_name_and_guild_name(relay):
    channel = SimpleNamespace(id=555, name="off-list-chan")
    guild = SimpleNamespace(id=42, name="Test Guild")
    payload = asyncio.run(_send(relay, channel, guild, {"general": {"id": "1"}}))
    assert payload["channel"] == "off-list-chan"
    assert payload["channel_id"] == "555"
    assert payload["server"] == "Test Guild"
    assert payload["guild_id"] == "42"


def test_thread_under_listed_channel_reads_as_parent_slash_thread(relay):
    channel = SimpleNamespace(id=556, name="side-quest", parent_id=1,
                              parent=SimpleNamespace(id=1, name="general"))
    guild = SimpleNamespace(id=42, name="Test Guild")
    payload = asyncio.run(_send(relay, channel, guild, {"general": {"id": "1"}}))
    assert payload["channel"] == "general/side-quest"
    assert payload["thread_parent_id"] == "1"
    assert payload["server"] == "Test Guild"


def test_message_outside_a_guild_is_server_dm(relay):
    channel = SimpleNamespace(id=557, name=None)
    payload = asyncio.run(_send(relay, channel, None, {}))
    assert payload["server"] == "dm"
    assert payload["guild_id"] is None
    assert payload["channel"] == "unknown"
