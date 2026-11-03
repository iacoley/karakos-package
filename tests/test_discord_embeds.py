"""Fenced `embed` blocks in replies (lib/discord_embeds.py, RADAGAST-A).

The pure cases need nothing but the module. The last group drives the reply
path of bin/agent-server.py with a recording stub for Discord's REST calls. No network.
"""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import discord_embeds as de  # noqa: E402

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
CHANNEL = "555000555"


def block(obj):
    return "```embed\n" + json.dumps(obj) + "\n```"


# -- extraction ---------------------------------------------------------------

def test_one_object_becomes_one_embed_and_no_content():
    content, embeds = de.extract_embed_blocks(block({"title": "Build 42", "color": 3066993}))
    assert content == ""
    assert embeds == [{"title": "Build 42", "color": 3066993}]


def test_array_becomes_several_embeds():
    text = block([{"title": "a"}, {"description": "b"}])
    content, embeds = de.extract_embed_blocks(text)
    assert content == ""
    assert [e.get("title") or e.get("description") for e in embeds] == ["a", "b"]


def test_block_mixed_with_prose_splits_content_and_embeds():
    text = "Status below.\n\n" + block({"title": "Deploy", "fields": [
        {"name": "Env", "value": "stage", "inline": True}]}) + "\n\nTell me if it looks wrong."
    content, embeds = de.extract_embed_blocks(text)
    assert content == "Status below.\n\nTell me if it looks wrong."
    assert embeds == [{"title": "Deploy",
                       "fields": [{"name": "Env", "value": "stage", "inline": True}]}]


@pytest.mark.parametrize("text", [
    "```embed\n{not json}\n```",                       # bad JSON: whole reply stays text
    "Done.\n```embed\n{\"title\": \"ok\"}\n```\n```embed\n[oops\n```",   # one bad block spoils all
    "```embed\n{}\n```",                               # empty object
    "```embed\n{\"unknown\": 1}\n```",                 # no supported field
    "```embed\n[]\n```",                               # empty array
    "```embed\n\"just a string\"\n```",
    "```python\nprint('hi')\n```",                      # not an embed block
])
def test_unusable_reply_stays_plain_text(text):
    assert de.extract_embed_blocks(text) == (text, [])


def test_plain_reply_is_untouched():
    text = "No blocks here, just `code` and ```\nfence\n```"
    assert de.extract_embed_blocks(text) == (text, [])


def test_split_reply_disabled_leaves_text_alone():
    text = "hello\n" + block({"title": "x"})
    assert de.split_reply(text, enabled=False) == (text, [])
    assert de.split_reply(text, enabled=True) == ("hello", [{"title": "x"}])


# -- clip limits: each value at the limit is kept, one over is cut -------------

def test_title_clipped_to_256():
    assert de.clip_embed({"title": "t" * 256})["title"] == "t" * 256
    assert len(de.clip_embed({"title": "t" * 257})["title"]) == 256


def test_description_clipped_to_4096():
    assert len(de.clip_embed({"description": "d" * 4097})["description"]) == 4096


def test_fields_capped_at_25():
    fields = [{"name": "n", "value": "v"}] * 26
    assert len(de.clip_embed({"fields": fields})["fields"]) == 25


def test_field_name_clipped_to_256_and_value_to_1024():
    f = de.clip_embed({"fields": [{"name": "n" * 257, "value": "v" * 1025}]})["fields"][0]
    assert (len(f["name"]), len(f["value"])) == (256, 1024)
    f = de.clip_embed({"fields": [{"name": "n" * 256, "value": "v" * 1024}]})["fields"][0]
    assert (len(f["name"]), len(f["value"])) == (256, 1024)


def test_footer_text_clipped_to_2048():
    assert len(de.clip_embed({"footer": {"text": "f" * 2049}})["footer"]["text"]) == 2048
    assert len(de.clip_embed({"footer": {"text": "f" * 2048}})["footer"]["text"]) == 2048


def test_message_total_capped_at_6000_characters():
    big = {"title": "t" * 256, "description": "d" * 4096,
           "fields": [{"name": "n" * 256, "value": "v" * 1024}] * 25}
    _, embeds = de.extract_embed_blocks(block([big, big, big]))
    total = sum(len(e.get("title", "")) + len(e.get("description", ""))
                + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))
                + len((e.get("footer") or {}).get("text", "")) for e in embeds)
    assert total == 6000


def test_message_total_at_limit_is_kept_whole_and_one_over_is_cut():
    # Two descriptions of 4096 each are 8192; the message keeps 6000 of them.
    _, embeds = de.extract_embed_blocks(block([{"description": "d" * 4096},
                                               {"description": "d" * 1904}]))
    assert [len(e["description"]) for e in embeds] == [4096, 1904]
    _, embeds = de.extract_embed_blocks(block([{"description": "d" * 4096},
                                               {"description": "d" * 1905}]))
    assert [len(e["description"]) for e in embeds] == [4096, 1904]


def test_ten_embed_cap_per_message():
    _, embeds = de.extract_embed_blocks(block([{"title": str(i)} for i in range(11)]))
    assert [e["title"] for e in embeds] == [str(i) for i in range(10)]


def test_ten_cap_counts_across_blocks():
    text = "\n".join(block([{"title": f"a{i}"} for i in range(6)]) for _ in range(2))
    _, embeds = de.extract_embed_blocks(text)
    assert len(embeds) == 10


# -- field shape ----------------------------------------------------------------

def test_footer_string_converts_to_text_object():
    assert de.clip_embed({"title": "x", "footer": "built on stage"})["footer"] == {
        "text": "built on stage"}


def test_unknown_keys_drop():
    out = de.clip_embed({"title": "x", "author": {"name": "a"}, "thumbnail": {"url": "u"},
                         "color": 1, "bogus": True})
    assert set(out) == {"title", "color"}


def test_color_must_be_an_int_in_range():
    assert "color" not in de.clip_embed({"title": "x", "color": "#ff0000"})
    assert "color" not in de.clip_embed({"title": "x", "color": True})
    assert "color" not in de.clip_embed({"title": "x", "color": 0x1000000})
    assert de.clip_embed({"title": "x", "color": 0xFFFFFF})["color"] == 0xFFFFFF


def test_bad_timestamp_and_url_drop_but_good_ones_keep():
    out = de.clip_embed({"title": "x", "timestamp": "yesterday", "url": "javascript:alert(1)"})
    assert out == {"title": "x"}
    out = de.clip_embed({"title": "x", "timestamp": "2026-11-03T12:00:00Z",
                         "url": "https://example.com/r"})
    assert out["timestamp"] == "2026-11-03T12:00:00Z" and out["url"] == "https://example.com/r"


def test_field_missing_name_or_value_is_dropped():
    out = de.clip_embed({"fields": [{"name": "a"}, {"value": "b"}, {"name": "c", "value": "d"}]})
    assert out["fields"] == [{"name": "c", "value": "d"}]


# -- agent-server reply path -------------------------------------------------------

class Resp:
    def __init__(self, status, data):
        self.status, self._data = status, data

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MsgRest:
    """Records every channel-messages body."""

    def __init__(self):
        self.bodies = []

    def post(self, url, **kw):
        if str(url).endswith("/messages"):
            body = kw.get("json")
            if body is None:
                body = {"_form": True}
            self.bodies.append(body)
        return Resp(200, {"id": f"d-{len(self.bodies)}"})


@pytest.fixture
def ags(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location("ags_embeds_under_test", AGENT_SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ags_embeds_under_test"] = module
    spec.loader.exec_module(module)
    module.agent_config = {"amos": {"model": "sonnet"}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    module.POST_RETRY_BASE_SEC = 0.0
    module.OUTBOX_PATH = tmp_path / "data" / "outbox" / "outbox.db"
    return module


def reply_setup(ags, channel_ux=None):
    ags.channels_config = {"channels": {"general": {"id": CHANNEL, "ux": channel_ux or {}}}}
    ags.http_session = MsgRest()
    return ags.http_session


def test_embed_only_reply_posts_embeds_without_content(ags):
    rest = reply_setup(ags)
    asyncio.run(ags.post_to_discord("amos", CHANNEL, block({"title": "Status", "fields": [
        {"name": "Tests", "value": "pass"}]}), dead_letter=True))
    assert len(rest.bodies) == 1
    body = rest.bodies[0]
    assert "content" not in body
    assert body["embeds"] == [{"title": "Status", "fields": [{"name": "Tests", "value": "pass"}]}]


def test_mixed_reply_posts_prose_as_content_with_embeds(ags):
    rest = reply_setup(ags)
    text = "Deploy finished.\n" + block({"title": "stage", "color": 3066993})
    asyncio.run(ags.post_to_discord("amos", CHANNEL, text, dead_letter=True))
    assert rest.bodies[0]["content"] == "Deploy finished."
    assert rest.bodies[0]["embeds"] == [{"title": "stage", "color": 3066993}]


def test_plain_text_reply_has_no_embeds_key(ags):
    rest = reply_setup(ags)
    asyncio.run(ags.post_to_discord("amos", CHANNEL, "```embed\n{bad\n```", dead_letter=True))
    assert rest.bodies[0]["content"] == "```embed\n{bad\n```"
    assert "embeds" not in rest.bodies[0]


def test_suppress_embeds_on_posts_text_untouched(ags):
    rest = reply_setup(ags, channel_ux={"suppress_embeds": True})
    text = "Status\n" + block({"title": "stage"})
    asyncio.run(ags.post_to_discord("amos", CHANNEL, text, dead_letter=True))
    assert rest.bodies[0]["content"] == text
    assert "embeds" not in rest.bodies[0] and rest.bodies[0]["flags"] == 4


def test_embeds_switch_off_posts_text_untouched(ags):
    rest = reply_setup(ags, channel_ux={"embeds": False})
    text = "Status\n" + block({"title": "stage"})
    asyncio.run(ags.post_to_discord("amos", CHANNEL, text, dead_letter=True))
    assert rest.bodies[0]["content"] == text and "embeds" not in rest.bodies[0]


def test_direct_path_never_extracts(ags):
    rest = reply_setup(ags)
    text = "tool line\n" + block({"title": "x"})
    asyncio.run(ags.post_to_discord("amos", CHANNEL, text))
    assert rest.bodies[0]["content"] == text and "embeds" not in rest.bodies[0]
