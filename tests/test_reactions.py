"""Agent emoji reactions (#285): `REACT: <emoji>` replies and the discord tool's
`react` action. The pure parts, the REST call, the turn loop and the HTTP route."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import discord_ux as ux  # noqa: E402

from conftest import import_script  # noqa: E402

THUMB = "%F0%9F%91%8D"
CUSTOM = "party:123456789012345678"
REAL_ID = "900000000000000001"


# -- pure parts -------------------------------------------------------------

def test_reactions_default_on_and_switchable():
    assert ux.parse_ux({})[0].for_channel("general").reactions is True
    cfg = {"ux": {"reactions": False},
           "channels": {"a": {"id": "1", "ux": {"reactions": True}}, "b": {"id": "2"}}}
    u = ux.parse_ux(cfg)[0]
    assert u.for_channel("a").reactions is True      # channel overrides the top level
    assert u.for_channel("b").reactions is False     # inherits the top level


def test_bad_reactions_value_warns_and_turns_off():
    u, warnings = ux.parse_ux({"ux": {"reactions": "yes"}})
    assert u.for_channel(None).reactions is False
    assert len(warnings) == 1 and "reactions" in warnings[0]


@pytest.mark.parametrize("text,expected", [
    ("REACT: 👍", THUMB),
    ("  REACT:👍\n", THUMB),
    (f"REACT: {CUSTOM}", CUSTOM),
    ("REACT: 1️⃣", "1%EF%B8%8F%E2%83%A3"),
    ("REACT: thanks", None),
    ("REACT: 👍 nice", None),
    ("react: 👍", None),
    ("Sure. REACT: 👍", None),
    ("REACT:", None),
    ("REACT: " + "👍" * 40, None),
    (None, None),
])
def test_parse_react_reply(text, expected):
    assert ux.parse_react_reply(text) == expected


# -- the REST call ----------------------------------------------------------

class _Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return "{}"


class _Rest:
    def __init__(self, *statuses):
        self.calls, self._statuses = [], list(statuses) or [204]

    def put(self, url, headers=None, **kw):
        self.calls.append((url, headers))
        return _Resp(self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0],
                     {"retry_after": 0})


@pytest.fixture
def ags(tmp_path):
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("ags_reactions_under_test",
                                                      PACKAGE_ROOT / "bin" / "agent-server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_reactions_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    return module


def test_add_reaction_puts_the_encoded_emoji(ags):
    ags.http_session = _Rest(204)
    assert asyncio.run(ags.add_reaction("amos", "55", REAL_ID, THUMB)) is True
    (url, headers), = ags.http_session.calls
    assert url == f"https://discord.com/api/v10/channels/55/messages/{REAL_ID}/reactions/{THUMB}/@me"
    assert headers["Authorization"] == "Bot bot-token-amos"


def test_custom_emoji_is_name_colon_id_in_the_path(ags):
    ags.http_session = _Rest(204)
    asyncio.run(ags.add_reaction("amos", "55", REAL_ID, ux.emoji_path(CUSTOM)))
    assert ags.http_session.calls[0][0].endswith(f"/reactions/{CUSTOM}/@me")


def test_add_reaction_retries_once_on_429_and_never_raises(ags):
    ags.http_session = _Rest(429, 204)
    assert asyncio.run(ags.add_reaction("amos", "55", REAL_ID, THUMB)) is True
    assert len(ags.http_session.calls) == 2
    ags.http_session = _Rest(403)
    assert asyncio.run(ags.add_reaction("amos", "55", REAL_ID, THUMB)) is False


# -- the turn loop and the HTTP route ---------------------------------------

def _send(h, text, message_id, channel_id="1"):
    async def go():
        resp = await h.client.post(
            "/message", headers=h._headers(),
            json={"agent": "a", "content": text, "channel_id": channel_id, "server": "local",
                  "author": "harness", "message_id": message_id})
        assert resp.status == 202, await resp.text()
    return go()


def _record(h):
    seen = []

    async def add_reaction(agent, channel_id, message_id, emoji_path):
        seen.append((agent, channel_id, message_id, emoji_path))
        return True
    h.module.add_reaction = add_reaction
    return seen


def test_react_reply_is_one_reaction_and_no_post(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            seen = _record(h)
            h.script(default={"text": "REACT: 👍"})
            await _send(h, "ping", REAL_ID)
            await h.wait_idle("a")
            return seen

    assert asyncio.run(scenario()) == [("a", "1", REAL_ID, THUMB)]
    assert h.discord == []


def test_switch_off_posts_the_text(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            seen = _record(h)
            h.module.channels_config = {"ux": {"reactions": False}}
            h.script(default={"text": "REACT: 👍"})
            await _send(h, "ping", REAL_ID)
            await h.wait_idle("a")
            return seen

    assert asyncio.run(scenario()) == []
    assert [m["content"] for m in h.discord] == ["REACT: 👍"]


def test_synthetic_message_id_gets_no_request_and_no_post(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            seen = _record(h)
            h.script(default={"text": "REACT: 👍"})
            await _send(h, "ping", "reaction:1:2:3")
            await h.wait_idle("a")
            return seen

    assert asyncio.run(scenario()) == []
    assert h.discord == []


def test_react_route_respects_the_switch_and_validates(harness):
    h = harness(agents=["a"])

    async def post(body):
        resp = await h.client.post("/react", headers=h._headers(), json=body)
        return resp.status, await resp.json()

    async def scenario():
        async with h:
            seen = _record(h)
            ok = await post({"agent": "a", "channel_id": "5", "message_id": REAL_ID, "emoji": "🎉"})
            bad = await post({"agent": "a", "channel_id": "5", "message_id": REAL_ID, "emoji": "yay"})
            h.module.channels_config = {"ux": {"reactions": False}}
            off = await post({"agent": "a", "channel_id": "5", "message_id": REAL_ID, "emoji": "🎉"})
            return seen, ok, bad, off

    seen, ok, bad, off = asyncio.run(scenario())
    assert ok[0] == 200 and bad[0] == 400
    assert off[0] == 403 and "disabled" in off[1]["error"]
    assert len(seen) == 1 and seen[0][2] == REAL_ID


# -- the tool action --------------------------------------------------------

@pytest.fixture
def tools_server(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def test_discord_tool_react_calls_the_route(tools_server, monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "channels.json").write_text('{"channels": {"general": {"id": "777"}}}')
    sent = []

    def fake(method, path, payload=None, timeout=15.0):
        sent.append((method, path, payload))
        return 200, {"reacted": True}
    monkeypatch.setattr(tools_server, "agent_server_request", fake)
    out = tools_server.handle_core_tool("discord", {"action": "react", "emoji": "👍", "channel": "general"})
    assert out == {"reacted": True}
    assert sent[0][:2] == ("POST", "/react")
    assert sent[0][2]["channel_id"] == "777" and sent[0][2]["emoji"] == "👍"
    assert sent[0][2]["message_id"] is None            # the server defaults to the turn's message


def test_discord_tool_react_reports_a_disabled_switch(tools_server, monkeypatch):
    monkeypatch.setattr(tools_server, "agent_server_request",
                        lambda *a, **k: (403, {"error": "reactions disabled for 5"}))
    out = tools_server.handle_core_tool("discord", {"action": "react", "emoji": "👍"})
    assert out == {"error": "reactions disabled for 5"}
