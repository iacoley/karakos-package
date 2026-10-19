"""Pure parts of the optional Discord behaviours (lib/discord_ux.py, step 6.2)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import discord_ux as ux  # noqa: E402
import registry  # noqa: E402
from routing import Route, route_message  # noqa: E402


def parse(cfg):
    return ux.parse_ux(cfg)


def chan(cfg, name="general"):
    u, _ = parse(cfg)
    return u.for_channel(name)


# -- switches ---------------------------------------------------------------

def test_defaults_all_off():
    c = chan({"channels": {"general": {"id": "1"}}})
    assert (c.threads, c.reaction_notices, c.edit_reroute, c.suppress_embeds) == (
        None, None, None, False)
    assert parse({})[1] == [] and parse(None)[1] == []


def test_install_wide_on_and_channel_off():
    cfg = {"ux": {"suppress_embeds": True, "threads": True},
           "channels": {"general": {"id": "1", "ux": {"suppress_embeds": False}},
                        "ops": {"id": "2"}}}
    assert chan(cfg, "general").suppress_embeds is False
    assert chan(cfg, "general").threads == ux.ThreadCfg()      # inherited
    assert chan(cfg, "ops").suppress_embeds is True
    assert chan(cfg, None).suppress_embeds is True              # unlisted: install-wide


def test_channel_on_and_install_wide_off():
    cfg = {"ux": {"suppress_embeds": False},
           "channels": {"general": {"id": "1", "ux": {
               "threads": {"after_s": 30, "max_lines": 20},
               "edit_reroute": {"window_s": 60, "max_followups": 1},
               "reaction_notices": "humans"}}}}
    c = chan(cfg)
    assert c.threads == ux.ThreadCfg(30.0, 20)
    assert c.edit_reroute == ux.EditCfg(60.0, 1)
    assert c.reaction_notices == "humans"
    assert chan(cfg, "other").threads is None


def test_reaction_notice_values():
    for v, want in ((True, "owner"), ("owner", "owner"), ("humans", "humans"),
                    (False, None)):
        assert chan({"ux": {"reaction_notices": v}}).reaction_notices == want


@pytest.mark.parametrize("key,bad", [
    ("threads", "yes"), ("threads", 5), ("threads", {"after_s": "x"}),
    ("threads", {"nope": 1}), ("threads", {"max_lines": 0}),
    ("reaction_notices", "everyone"), ("reaction_notices", 3),
    ("edit_reroute", "on"), ("edit_reroute", {"window_s": -1}),
    ("suppress_embeds", "true"), ("suppress_embeds", 1),
])
def test_wrong_type_warns_once_and_is_off(key, bad):
    u, warnings = parse({"ux": {key: bad}})
    assert len(warnings) == 1 and key in warnings[0]
    c = u.for_channel("general")
    assert (c.threads, c.reaction_notices, c.edit_reroute, c.suppress_embeds) == (
        None, None, None, False)


def test_bad_channel_value_is_off_even_when_install_wide_is_on():
    cfg = {"ux": {"suppress_embeds": True},
           "channels": {"g": {"id": "1", "ux": {"suppress_embeds": "yes"}}}}
    u, warnings = parse(cfg)
    assert len(warnings) == 1 and "channel g" in warnings[0]
    assert u.for_channel("g").suppress_embeds is False


def test_unknown_key_and_non_object_warn():
    assert len(parse({"ux": {"sparkles": True}})[1]) == 1
    assert len(parse({"ux": "on"})[1]) == 1
    assert len(parse({"channels": {"g": {"id": "1", "ux": [1]}}})[1]) == 1


def test_tool_streaming_is_per_channel_and_inherits_when_unset():
    cfg = {"ux": {"tool_streaming": True},
           "channels": {"quiet": {"id": "1", "ux": {"tool_streaming": False}},
                        "loud": {"id": "2"}}}
    u, warnings = parse(cfg)
    assert warnings == []
    assert u.for_channel("quiet").tool_streaming is False
    assert u.for_channel("loud").tool_streaming is True      # top-level, inherited
    assert u.for_channel(None).tool_streaming is True


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, ["true"]])
def test_tool_streaming_non_bool_warns_and_is_ignored(bad):
    cfg = {"channels": {"general": {"id": "1", "ux": {"tool_streaming": bad}}}}
    u, warnings = parse(cfg)
    assert len(warnings) == 1
    assert "tool_streaming" in warnings[0] and "must be true or false" in warnings[0]
    assert u.for_channel("general").tool_streaming is None   # inherit the agent


def test_tool_streaming_unlisted_channel_is_none():
    u, _ = parse({"channels": {"general": {"id": "1"}}})
    assert u.for_channel("nope").tool_streaming is None
    assert u.for_channel(None).tool_streaming is None
    assert chan({}).tool_streaming is None


def test_suppress_embeds_matches_discord_py():
    discord = pytest.importorskip("discord")
    assert ux.SUPPRESS_EMBEDS == discord.MessageFlags.suppress_embeds.flag == 4


# -- text -------------------------------------------------------------------

def test_thread_name():
    assert ux.thread_name("fix the build\nplease") == "Working on: fix the build please"
    assert ux.thread_name("") == "Working on: your request"
    assert ux.thread_name("   \n ") == "Working on: your request"
    long = ux.thread_name("x" * 500)
    assert long == "Working on: " + "x" * 60 and len(long) <= 100
    name = ux.thread_name("@everyone and @here look")
    assert "@everyone" not in name and "@here" not in name and "​" in name
    assert len(ux.thread_name("@" * 500)) <= 100


def test_reaction_notice_text():
    assert ux.reaction_notice_text("Sam", "👍", "ship it\nnow") == (
        '[reaction, no reply needed unless it changes something] '
        'Sam reacted 👍 to your message: "ship it now"')
    out = ux.reaction_notice_text("Sam", "✅", "y" * 300)
    snippet = out.split('message: "', 1)[1][:-1]
    assert len(snippet) <= 140


def test_edit_followup_text():
    t = ux.edit_followup_text("Sam", "a" * 600, "b" * 1200, "during")
    head, before, after = t.split("\n")
    assert head == "[Sam edited their earlier message while you were working on it]"
    assert before == "Before: " + "a" * 500 and after == "After: " + "b" * 1000
    assert ux.edit_followup_text("Sam", "x", "y", "after").startswith(
        "[Sam edited their earlier message after you answered it]")


def test_lru_map():
    m = ux.LruMap(3)
    for i in range(5):
        m[i] = f"p{i}"
    assert len(m) == 3 and 0 not in m and m.get("4") == "p4" and m.get(99) is None


# -- LongTurn -----------------------------------------------------------------

def lt(**kw):
    return ux.LongTurn(ux.ThreadCfg(60, kw.pop("max_lines", 40)), "100", 0.0,
                       "do the thing", 5, 12)


def test_long_turn_below_threshold_stays_in_channel():
    t = lt()
    assert t.plan(0, 0, None) == "post"
    t.note_post("m1")
    assert t.plan(3, 1, 0) == "skip"            # min interval
    assert t.plan(10, 1, 0) == "post" and t.target == "100"
    assert t.plan(50, 11, 40) == "post"
    assert t.plan(55, 12, 50) == "skip"         # channel cap


def test_long_turn_threads_after_threshold_and_raises_cap():
    t = lt()
    t.note_post("m1")
    assert t.plan(61, 5, 55) == "thread"
    t.thread_created("T1")
    assert t.target == "T1"
    assert t.plan(70, 12, 65) == "post"         # line 13 allowed in thread mode
    assert t.plan(70, 40, 65) == "skip"


def test_long_turn_without_anchor_or_after_failure_never_threads():
    t = lt()
    assert t.plan(100, 1, 0) == "post"          # no anchor yet
    t.note_post("m1")
    t.thread_failed()
    assert t.plan(100, 5, 0) == "post" and t.plan(100, 12, 0) == "skip"
    assert t.target == "100"


def test_anchor_is_first_post_only():
    t = lt()
    t.note_post(None)
    t.note_post("m1")
    t.note_post("m2")
    assert t.anchor == "m1"


# -- edit_decision --------------------------------------------------------------

def row(**kw):
    base = {"author_id": "7", "content": "old", "processed": 0, "response": None,
            "created_at_ts": 1000}
    base.update(kw)
    return base


def dec(r, followups=(), now=1100, **kw):
    kw.setdefault("author_id", "7")
    kw.setdefault("new_text", "new")
    return ux.edit_decision(r, now, 900, 3, list(followups), **kw)


def test_edit_decision_table():
    assert dec(None).status == "unknown"
    assert dec(row(), author_id="8").status == "author_mismatch"
    assert dec(row(), now=1000 + 901).status == "too_old"
    assert dec(row(), new_text="old").status == "unchanged"
    assert dec(row()).action == "update"
    d = dec(row(processed=1))
    assert (d.action, d.phase) == ("followup", "during")
    for st in (2, 3, 4):
        d = dec(row(processed=st, response="the answer"))
        assert (d.action, d.phase) == ("followup", "after")
        assert dec(row(processed=st, response="PASS")).status == "ignored"
        assert dec(row(processed=st, response="  ")).status == "ignored"
        assert dec(row(processed=st, response=None)).status == "ignored"


def test_edit_decision_followup_limits():
    r = row(processed=1)
    done = [{"message_id": f"edit:x:{i}", "processed": 2} for i in (1, 2, 3)]
    assert dec(r, done).status == "refused"
    queued = done[:2] + [{"message_id": "edit:x:3", "processed": 0}]
    d = dec(r, queued)
    assert d.action == "update_followup" and d.followup_id == "edit:x:3"
    assert dec(r, done[:2]).action == "followup"


# -- routing: a thread resolves through its parent --------------------------------

def test_routing_resolves_thread_through_parent():
    reg = registry.parse_registry({"version": 2, "agents": {
        "a": {"name": "a", "role": "primary", "shards": [
            {"id": "a", "channels": ["general"]}]},
        "m": {"name": "m", "role": "monitor"}}})
    assert route_message(reg, None, None, False, parent_channel_name="general") == Route(
        "a", "a", "channel")
    assert route_message(reg, None, None, False, parent_channel_name=None) is None
    assert route_message(reg, None, "a", False) is None
    assert route_message(reg, "general", None, False, parent_channel_name="zzz") == Route(
        "a", "a", "channel")
