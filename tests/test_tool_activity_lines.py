"""
Tests for #91 (tool-call activity half): a four-minute turn is
indistinguishable from a hung one.

The package already parses its own stream-json output and already sees every
`tool_use` block — it logged the tool name and then, behind `tool_streaming`,
posted a bare "🔧 Bash". That flag was dead: no config file, template, doc or
test in this repo ever set it, so on every install the branch was
unreachable. The observation point was fully wired at one end and had nothing
downstream, which is the same shape as #64 and #90.

What the acceptance test asks for is `-# ⚙ Bash — …` lines in the channel
before the final reply, so these tests are about three things: the line says
which tool AND on what, it actually fires by default, and a turn making fifty
tool calls does not post fifty messages.
"""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"

pytest.importorskip("aiohttp")

CHANNEL = "555000555"


def _load(name, path, workspace):
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


@pytest.fixture
def ags(tmp_path):
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    module = _load("ags_tool_lines_under_test", AGENT_SERVER, tmp_path)
    module.agent_config = {"amos": {"model": "sonnet"}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    return module


class FakeStdin:
    def write(self, data):
        pass

    async def drain(self):
        return None


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class FakeProc:
    def __init__(self, stdout):
        self.stdin = FakeStdin()
        self.stdout = stdout
        self.pid = 4242


def assistant_event(*tool_calls):
    """A stream-json assistant event carrying tool_use blocks."""
    return json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": name, "input": tool_input}
            for name, tool_input in tool_calls
        ]},
    }).encode() + b"\n"


def result_event(text="done"):
    return json.dumps({
        "type": "result", "session_id": "s1", "result": text,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode() + b"\n"


def drive(ags, lines, *, channel_id=CHANNEL, config=None):
    """Run one turn's stream through read_agent_response, capturing posts."""
    posted = []

    async def fake_post(agent, cid, content, reply_to=None, dead_letter=False, queue_message_id=None):
        posted.append({"channel_id": cid, "content": content,
                       "dead_letter": dead_letter})
        return f"discord-{len(posted)}"

    ags.post_to_discord = fake_post
    if config is not None:
        ags.agent_config = {"amos": config}
    ags.agent_processes["amos"] = FakeProc(FakeStdout(lines))
    asyncio.run(ags.read_agent_response("amos", channel_id, ["msg-1"]))
    return posted


# --------------------------------------------------------------------------
# The line itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name,tool_input,expected", [
    ("Bash", {"command": "npm test"}, "-# ⚙ Bash — npm test"),
    ("Read", {"file_path": "/srv/app/main.py"}, "-# ⚙ Read — /srv/app/main.py"),
    ("Grep", {"pattern": "TODO"}, "-# ⚙ Grep — TODO"),
    ("WebFetch", {"url": "https://example.com"}, "-# ⚙ WebFetch — https://example.com"),
    ("Task", {"description": "audit the config"}, "-# ⚙ Task — audit the config"),
])
def test_summary_names_the_tool_and_what_it_is_working_on(ags, tool_name, tool_input, expected):
    assert ags.summarize_tool_call(tool_name, tool_input) == expected


def test_edit_reports_its_path_not_its_patch_body(ags):
    """Key order is load-bearing: an Edit carries the whole replacement text,
    and the path is the part a human reads."""
    line = ags.summarize_tool_call("Edit", {
        "file_path": "/srv/app/main.py",
        "old_string": "a" * 500,
        "new_string": "b" * 500,
    })
    assert line == "-# ⚙ Edit — /srv/app/main.py"


def test_unknown_tool_degrades_to_the_bare_name(ags):
    """Tool inputs carry file contents, patch bodies and credentials, and
    this line goes to a Discord channel. An unrecognised tool gets its name
    and nothing else — never a dump of its arguments."""
    line = ags.summarize_tool_call("mcp__vault__read", {
        "secret": "hunter2", "token": "ghp_realish", "body": "x" * 200,
    })
    assert line == "-# ⚙ mcp__vault__read"
    assert "hunter2" not in line and "ghp_realish" not in line


def test_long_and_multiline_detail_is_flattened_and_truncated(ags):
    line = ags.summarize_tool_call("Bash", {"command": "for i in 1 2 3; do\n  echo " + "x" * 200 + "\ndone"})
    assert "\n" not in line
    assert line.startswith("-# ⚙ Bash — ")
    assert line.endswith("…")
    assert len(line) < 130


def test_backticks_cannot_break_out_of_the_subtext_line(ags):
    line = ags.summarize_tool_call("Bash", {"command": "echo `whoami`"})
    assert "`" not in line


def test_missing_and_malformed_input_do_not_raise(ags):
    assert ags.summarize_tool_call("Bash", None) == "-# ⚙ Bash"
    assert ags.summarize_tool_call("Bash", {}) == "-# ⚙ Bash"
    assert ags.summarize_tool_call("Bash", "not-a-dict") == "-# ⚙ Bash"
    assert ags.summarize_tool_call("Bash", {"command": "   "}) == "-# ⚙ Bash"
    assert ags.summarize_tool_call(None, {}) == "-# ⚙ unknown"


# --------------------------------------------------------------------------
# Whether it fires at all
# --------------------------------------------------------------------------

def test_tool_lines_are_on_by_default(ags):
    """The whole defect. tool_streaming defaulted to False and nothing in
    this repo set it, so the branch was unreachable on every install."""
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()])
    assert [p["content"] for p in posted] == ["-# ⚙ Bash — npm test"]


def test_tool_lines_can_still_be_turned_off(ags):
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()],
                   config={"model": "sonnet", "tool_streaming": False})
    assert posted == []


def _listed(ags, ux):
    """Put CHANNEL in channels.json with a per-channel ux block."""
    ags.channels_config = {"channels": {"general": {"id": CHANNEL, "ux": ux}}}


def test_channel_override_false_silences_an_agent_that_streams(ags):
    _listed(ags, {"tool_streaming": False})
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()],
                   config={"model": "sonnet", "tool_streaming": True})
    assert posted == []


def test_channel_override_true_streams_for_an_agent_that_is_silent(ags):
    _listed(ags, {"tool_streaming": True})
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()],
                   config={"model": "sonnet", "tool_streaming": False})
    assert [p["content"] for p in posted] == ["-# ⚙ Bash — npm test"]


def test_unset_channel_override_inherits_the_agent_setting(ags):
    _listed(ags, {})
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()],
                   config={"model": "sonnet", "tool_streaming": False})
    assert posted == []


def test_silent_mode_posts_nothing(ags):
    """channel_id "0" is the local/headless lane — there is no channel to
    reassure."""
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()], channel_id="0")
    assert posted == []


def test_tool_lines_are_not_dead_lettered(ags):
    """A liveness signal is worthless once the turn has ended; replaying it
    from the dead-letter queue later would be noise."""
    posted = drive(ags, [assistant_event(("Bash", {"command": "npm test"})),
                         result_event()])
    assert posted and all(p["dead_letter"] is False for p in posted)


def test_lines_land_before_the_final_reply(ags):
    """The acceptance test's ordering: the channel sees activity while the
    turn is running, not batched at the end."""
    posted = drive(ags, [
        assistant_event(("Bash", {"command": "npm test"})),
        result_event("all green"),
    ])
    # read_agent_response returns the reply for the caller to post, so
    # anything it posted itself necessarily preceded that.
    assert [p["content"] for p in posted] == ["-# ⚙ Bash — npm test"]


# --------------------------------------------------------------------------
# Not becoming the thing it fixes
# --------------------------------------------------------------------------

def test_a_burst_of_tool_calls_does_not_post_one_message_each(ags):
    """A turn can make dozens of tool calls in seconds. Posting each one
    would rate-limit the bot and bury the channel it is meant to reassure —
    the throttle is what makes default-on safe."""
    burst = assistant_event(*[("Bash", {"command": f"step {i}"}) for i in range(50)])
    posted = drive(ags, [burst, result_event()])

    assert len(posted) == 1, [p["content"] for p in posted]
    # The one that got through is the first, so the channel hears quickly.
    assert posted[0]["content"] == "-# ⚙ Bash — step 0"


def test_the_first_call_is_never_throttled(ags):
    """Liveness is the point: a turn whose first tool call is silent for
    five seconds is exactly the silence #91 is about."""
    ags.TOOL_EVENT_MIN_INTERVAL = 3600
    posted = drive(ags, [assistant_event(("Bash", {"command": "first"})),
                         result_event()])
    assert [p["content"] for p in posted] == ["-# ⚙ Bash — first"]


def test_first_line_is_not_throttled_on_a_freshly_booted_host(ags):
    """The one this file got wrong first time round.

    The throttle was written with a 0.0 "never posted" sentinel, so the
    first call's check read `now - 0.0 >= interval`. That is true on any
    host with more than `interval` seconds of uptime — 26694s on the box
    this was written on — so a mutation deleting the first-call exemption
    passed the whole suite. It would have shipped, and then suppressed the
    opening tool line of the first turn in a container in its first
    minutes, which is precisely the install the package targets.

    So this asserts against clock values, not against wall time: `now` here
    is what time.monotonic() actually returns seconds into a fresh boot.
    """
    ags.TOOL_EVENT_MIN_INTERVAL = 5

    assert ags.should_post_tool_line(0, None, 0.4) is True
    assert ags.should_post_tool_line(0, None, 2.0) is True

    # And once a line has gone out, the interval applies normally — the
    # exemption is about the sentinel, not about small clock values.
    assert ags.should_post_tool_line(1, 0.4, 2.0) is False
    assert ags.should_post_tool_line(1, 0.4, 6.0) is True


def test_the_cap_beats_the_interval(ags):
    """Both conditions have to hold, and the ceiling is the one that stops
    an hour-long turn whose calls are minutes apart."""
    ags.TOOL_EVENT_MIN_INTERVAL = 5
    at_cap = ags.TOOL_EVENT_MAX_PER_TURN

    assert ags.should_post_tool_line(at_cap - 1, 0.0, 10_000.0) is True
    assert ags.should_post_tool_line(at_cap, 0.0, 10_000.0) is False
    # Not even the sentinel gets past the ceiling.
    assert ags.should_post_tool_line(at_cap, None, 10_000.0) is False


def test_a_long_turn_is_capped_even_when_calls_are_spread_out(ags):
    """With the interval satisfied every time, only the per-turn ceiling
    stops an hour-long turn from posting hundreds of lines."""
    ags.TOOL_EVENT_MIN_INTERVAL = 0
    events = [assistant_event(("Bash", {"command": f"step {i}"})) for i in range(40)]
    posted = drive(ags, events + [result_event()])
    assert len(posted) == ags.TOOL_EVENT_MAX_PER_TURN


def test_the_throttle_resets_between_turns(ags):
    """Per-turn state, not global — otherwise the second turn of a busy day
    is silent, which is the bug again with extra steps."""
    ags.TOOL_EVENT_MIN_INTERVAL = 3600

    first = drive(ags, [assistant_event(("Bash", {"command": "turn one"})),
                        result_event()])
    second = drive(ags, [assistant_event(("Bash", {"command": "turn two"})),
                         result_event()])

    assert [p["content"] for p in first] == ["-# ⚙ Bash — turn one"]
    assert [p["content"] for p in second] == ["-# ⚙ Bash — turn two"]
