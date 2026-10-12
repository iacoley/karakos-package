"""
Tests for #148: cross-restart session continuity had never worked.

Three defects in one feature, each of which alone is fatal:

1. Nothing in the package ever WROTE logs/agent-streams/. bin/agent-server.py
   only mkdir'd the directory; bin/summarize-session.py globbed it, found
   nothing, and hit `if not stream_content: sys.exit(1)` every single run —
   so data/last-session-summary-{agent}.md was never written and the
   [SESSION RESET] re-injection in start_agent_subprocess never fired.
2. The summarizer parsed a flat {"type": "text"} event. Claude Code emits
   `assistant` events whose message.content is a list of blocks — the shape
   read_agent_response() in bin/agent-server.py has always parsed correctly.
   So even given a log, every line fell through and the summary was empty.
3. The MCP `session` tool's finalize action invoked summarize-session.py with
   no arguments, against a script declaring `agent` as a required positional:
   argparse exit 2, reported as a bare empty "output".

The tests are grouped by defect, and the middle group is the one that
matters most — it drives a real turn through the tee and then reads the
resulting file back with the real summarizer, which is the end-to-end path
that was broken.
"""

import asyncio
import json
import os
import re
import time

import pytest

from conftest import import_script, PACKAGE_ROOT

pytest.importorskip("aiohttp")


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture
def ags(tmp_path, monkeypatch):
    """bin/agent-server.py with WORKSPACE_ROOT pointed at a scratch dir."""
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    module = import_script("agent-server")
    module.agent_config = {"amos": {"model": "sonnet", "tool_streaming": False}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    yield module
    for entry in list(module._stream_log_files.values()):
        try:
            entry["fh"].close()
        except Exception:
            pass
    module._stream_log_files.clear()


@pytest.fixture
def summarizer(tmp_path, monkeypatch):
    """bin/summarize-session.py against the same scratch workspace layout."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("summarize-session")


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


def assistant_event(*blocks):
    """A realistic stream-json assistant event: message.content is a LIST OF
    BLOCKS, which is the shape the summarizer used not to understand."""
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-sonnet-4",
                    "content": list(blocks)},
    }).encode() + b"\n"


def text_block(text):
    return {"type": "text", "text": text}


def tool_block(name, tool_input=None):
    return {"type": "tool_use", "id": "toolu_1", "name": name,
            "input": tool_input or {}}


def result_event(text="done"):
    return json.dumps({
        "type": "result", "subtype": "success", "session_id": "s1",
        "result": text, "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode() + b"\n"


def drive(ags, lines, *, agent="amos", channel_id="0"):
    """Run one turn's stream through read_agent_response."""
    async def fake_post(agent_, cid, content, reply_to=None, dead_letter=False):
        return "discord-1"

    ags.post_to_discord = fake_post
    ags.agent_processes[agent] = FakeProc(FakeStdout(lines))
    return asyncio.run(ags.read_agent_response(agent, channel_id, ["msg-1"]))


def stream_logs(ags, agent="amos"):
    return sorted(ags.STREAM_LOG_DIR.glob(f"{agent}_*.jsonl"))


# ---------------------------------------------------------------------------
# Defect 1: nothing ever wrote the stream log
# ---------------------------------------------------------------------------

class TestStreamLogIsWritten:

    def test_a_turn_writes_a_stream_log(self, ags):
        """The whole primary defect. STREAM_LOG_DIR was mkdir'd and read,
        never written, so the summarizer's glob matched nothing forever."""
        drive(ags, [assistant_event(text_block("hello")), result_event()])

        files = stream_logs(ags)
        assert len(files) == 1, [f.name for f in files]
        assert files[0].stat().st_size > 0

    def test_the_filename_matches_the_glob_the_summarizer_uses(self, ags, summarizer):
        """A log the summarizer cannot find is the same as no log. Asserted
        through the summarizer's own glob, not a copy of the pattern."""
        drive(ags, [assistant_event(text_block("hello")), result_event()])

        summarizer.STREAM_LOG_DIR = ags.STREAM_LOG_DIR
        assert list(summarizer.STREAM_LOG_DIR.glob("amos_*.jsonl"))

    def test_every_event_is_teed_verbatim(self, ags):
        """Raw lines, in order — the summarizer parses stream-json, so the
        tee must not reshape events on the way out."""
        events = [
            assistant_event(text_block("thinking out loud")),
            assistant_event(tool_block("Bash", {"command": "pytest -q"})),
            result_event("all green"),
        ]
        drive(ags, list(events))

        written = stream_logs(ags)[0].read_bytes().splitlines(keepends=True)
        assert written == list(events)

    def test_a_malformed_line_is_still_recorded(self, ags):
        """Teed before parsing: a line the JSON decoder rejects is exactly
        the line someone debugging a broken turn wants to see."""
        drive(ags, [b"not json at all\n",
                    assistant_event(text_block("hi")), result_event()])

        assert b"not json at all" in stream_logs(ags)[0].read_bytes()

    def test_the_log_is_readable_by_another_process_immediately(self, ags):
        """summarize-session.py runs as a SEPARATE process while the agent
        is still alive and holding the handle. A buffered write would leave
        it tailing a stale file — the failure would look like the original
        bug, and only under load."""
        ags.agent_processes["amos"] = FakeProc(FakeStdout([
            assistant_event(text_block("first")),
            assistant_event(text_block("second")),
            result_event(),
        ]))
        asyncio.run(ags.read_agent_response("amos", "0", ["msg-1"]))

        # Handle still open (no close between turns) — read it from scratch.
        assert ags._stream_log_files["amos"]["fh"].closed is False
        with open(stream_logs(ags)[0]) as fh:
            assert len(fh.readlines()) == 3

    def test_a_second_turn_appends_to_the_same_file(self, ags):
        drive(ags, [assistant_event(text_block("one")), result_event()])
        drive(ags, [assistant_event(text_block("two")), result_event()])

        files = stream_logs(ags)
        assert len(files) == 1
        assert len(files[0].read_text().splitlines()) == 4

    def test_each_agent_gets_its_own_file(self, ags):
        ags.agent_config["kara"] = {"model": "sonnet", "tool_streaming": False}
        drive(ags, [assistant_event(text_block("a")), result_event()], agent="amos")
        drive(ags, [assistant_event(text_block("k")), result_event()], agent="kara")

        assert len(stream_logs(ags, "amos")) == 1
        assert len(stream_logs(ags, "kara")) == 1
        assert b"\"k\"" not in stream_logs(ags, "amos")[0].read_bytes()

    def test_the_log_rolls_over_at_the_size_cap(self, ags):
        """A long-lived agent must not grow one unbounded file on a volume
        the rest of the household shares."""
        ags.STREAM_LOG_MAX_BYTES = 200
        for _ in range(6):
            drive(ags, [assistant_event(text_block("x" * 100)), result_event()])

        files = stream_logs(ags)
        assert len(files) > 1
        assert all(f.stat().st_size < 4 * ags.STREAM_LOG_MAX_BYTES for f in files)

    def test_the_summarizer_still_finds_the_newest_file_after_a_roll(self, ags, summarizer):
        """Rotation is only safe if the mtime sort still lands on the live
        file — otherwise the summary describes an hour-old session."""
        ags.STREAM_LOG_MAX_BYTES = 200
        for i in range(6):
            drive(ags, [assistant_event(text_block(f"turn {i} " + "x" * 100)),
                        result_event()])

        # Files written within one timestamp tick tie on mtime; give them the
        # strictly increasing mtimes real time would have (creation order: the
        # base name, then -1, -2, ...).
        files = list(ags.STREAM_LOG_DIR.glob("amos_*.jsonl"))
        def order(p):
            m = re.match(r"amos_(\d+-\d+)(?:-(\d+))?$", p.stem)
            return (m.group(1), int(m.group(2) or 0))
        files.sort(key=order)
        base = time.time() - 100
        for n, f in enumerate(files):
            os.utime(f, (base + n, base + n))
        summarizer.STREAM_LOG_DIR = ags.STREAM_LOG_DIR
        assert "turn 5" in summarizer.read_recent_stream("amos")

    def test_a_write_failure_never_costs_the_turn(self, ags):
        """Fail-safe is the requirement: this sits in the readline loop of
        every turn, and a full disk must cost the log line, not the reply."""
        def boom(agent):
            raise OSError("No space left on device")

        ags._open_stream_log = boom

        text, metadata = drive(ags, [assistant_event(text_block("still answers")),
                                     result_event()])
        assert text == "still answers"
        assert metadata["session_id"] == "s1"

    def test_a_broken_handle_is_dropped_and_retried(self, ags):
        """A handle that started failing must not poison every later turn."""
        drive(ags, [assistant_event(text_block("one")), result_event()])
        ags._stream_log_files["amos"]["fh"].close()   # simulate a dead fd

        drive(ags, [assistant_event(text_block("two")), result_event()])

        assert b"two" in b"".join(f.read_bytes() for f in stream_logs(ags))


# ---------------------------------------------------------------------------
# Defect 2: the summarizer parsed a shape the CLI does not emit
# ---------------------------------------------------------------------------

class TestSummarizerParsesTheRealShape:

    def test_assistant_text_blocks_are_extracted(self, summarizer):
        lines = summarizer.format_stream_event(json.loads(
            assistant_event(text_block("I rebuilt the dashboard")).decode()))
        assert lines == ["[TEXT] I rebuilt the dashboard"]

    def test_assistant_tool_use_blocks_are_extracted(self, summarizer):
        lines = summarizer.format_stream_event(json.loads(
            assistant_event(tool_block("Bash", {"command": "pytest"})).decode()))
        assert lines == ["[TOOL] Bash"]

    def test_a_mixed_block_list_keeps_order_and_drops_thinking(self, summarizer):
        event = json.loads(assistant_event(
            {"type": "thinking", "thinking": "let me check the logs"},
            text_block("Checking the logs."),
            tool_block("Read", {"file_path": "/workspace/logs/x"}),
        ).decode())
        assert summarizer.format_stream_event(event) == [
            "[TEXT] Checking the logs.",
            "[TOOL] Read",
        ]

    def test_the_flat_shape_it_used_to_expect_is_never_emitted(self, summarizer):
        """Proof this test file is testing the real defect: the old parser
        keyed on event["type"] == "text", and a real assistant event's top
        level type is "assistant" with the text buried in message.content."""
        event = json.loads(assistant_event(text_block("hi")).decode())
        assert event["type"] == "assistant"
        assert "text" not in event

    def test_the_flat_shape_still_parses_for_compatibility(self, summarizer):
        assert summarizer.format_stream_event(
            {"type": "text", "text": "legacy"}) == ["[TEXT] legacy"]
        assert summarizer.format_stream_event(
            {"type": "tool_use", "name": "Bash"}) == ["[TOOL] Bash"]

    def test_the_user_prompt_is_kept_but_tool_results_are_not(self, summarizer):
        """The prompt is the single most useful line in a summary; a
        tool_result block list is bulk the [TOOL] line already covers."""
        assert summarizer.format_stream_event({
            "type": "user", "message": {"role": "user", "content": "deploy it"},
        }) == ["[USER] deploy it"]
        assert summarizer.format_stream_event({
            "type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "x" * 5000}]},
        }) == []

    def test_long_text_is_truncated(self, summarizer):
        lines = summarizer.format_stream_event(json.loads(
            assistant_event(text_block("y" * 5000)).decode()))
        assert len(lines[0]) == len("[TEXT] ") + 200

    def test_bookkeeping_events_contribute_nothing(self, summarizer):
        for event in (
            {"type": "system", "subtype": "init", "tools": ["Bash"]},
            json.loads(result_event().decode()),
            {"type": "rate_limit_event", "rate_limit_info": {}},
        ):
            assert summarizer.format_stream_event(event) == []

    def test_malformed_events_do_not_raise(self, summarizer):
        assert summarizer.format_stream_event({"type": "assistant"}) == []
        assert summarizer.format_stream_event(
            {"type": "assistant", "message": {"content": None}}) == []
        assert summarizer.format_stream_event(
            {"type": "assistant", "message": {"content": ["not-a-dict"]}}) == []
        assert summarizer.format_stream_event({}) == []

    def test_read_recent_stream_returns_empty_when_nothing_was_logged(self, summarizer, tmp_path):
        """The pre-fix state, kept as a test so the exit(1) guard's trigger
        stays visible: no stream file means no summary, by design."""
        summarizer.STREAM_LOG_DIR = tmp_path / "logs" / "agent-streams"
        assert summarizer.read_recent_stream("amos") == ""

    def test_read_recent_stream_honours_the_line_limit(self, summarizer, tmp_path):
        d = tmp_path / "logs" / "agent-streams"
        d.mkdir(parents=True, exist_ok=True)
        (d / "amos_20260826-120000.jsonl").write_bytes(
            b"".join(assistant_event(text_block(f"line {i}")) for i in range(100)))
        summarizer.STREAM_LOG_DIR = d

        out = summarizer.read_recent_stream("amos", limit=5).splitlines()
        assert out == [f"[TEXT] line {i}" for i in range(95, 100)]

    def _two_files(self, tmp_path, old_lines, new_lines):
        d = tmp_path / "logs" / "agent-streams"
        d.mkdir(parents=True, exist_ok=True)
        old = d / "amos_20260101-000000.jsonl"
        new = d / "amos_20260826-120000.jsonl"
        old.write_bytes(b"".join(assistant_event(text_block(t)) for t in old_lines))
        new.write_bytes(b"".join(assistant_event(text_block(t)) for t in new_lines))
        os.utime(old, (time.time() - 86400, time.time() - 86400))
        return d

    def test_the_newest_file_is_read_last_and_older_ones_backfill(self, summarizer, tmp_path):
        """A roll can land mid-session, leaving the live file a handful of
        events long — reading only it would summarize the last thirty
        seconds of a six-hour session. Older first, newest last, so the
        summarizer reads the session in order."""
        summarizer.STREAM_LOG_DIR = self._two_files(tmp_path, ["older"], ["current"])

        assert summarizer.read_recent_stream("amos").splitlines() == [
            "[TEXT] older", "[TEXT] current",
        ]

    def test_backfill_stops_once_the_limit_is_full(self, summarizer, tmp_path):
        """The newest file alone satisfying the limit is the normal case —
        a rotated file must not push current events out of the window."""
        summarizer.STREAM_LOG_DIR = self._two_files(
            tmp_path, ["older"], ["a", "b", "c"])

        assert summarizer.read_recent_stream("amos", limit=3).splitlines() == [
            "[TEXT] a", "[TEXT] b", "[TEXT] c",
        ]

    def test_lookback_is_bounded(self, summarizer, tmp_path):
        """Retention keeps a week of these; a summary must not walk all of
        them just because each is short."""
        d = tmp_path / "logs" / "agent-streams"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            f = d / f"amos_2026082{i}-000000.jsonl"
            f.write_bytes(assistant_event(text_block(f"file {i}")))
            os.utime(f, (time.time() - (10 - i) * 3600,) * 2)
        summarizer.STREAM_LOG_DIR = d

        out = summarizer.read_recent_stream("amos").splitlines()
        assert len(out) == summarizer.STREAM_FILE_LOOKBACK
        assert out[-1] == "[TEXT] file 9"

    # -- the end-to-end path, which is the actual bug ----------------------

    def test_a_real_turn_produces_a_summarizable_stream(self, ags, summarizer):
        """The regression that matters: drive a turn through agent-server's
        tee, then read it back with the summarizer's own reader. Before the
        fix this returned "" — no file to read and no parser for its
        contents — which tripped `sys.exit(1)` and meant
        data/last-session-summary-{agent}.md was never written."""
        drive(ags, [
            json.dumps({"type": "user",
                        "message": {"role": "user", "content": "ship the fix"}}).encode() + b"\n",
            assistant_event({"type": "thinking", "thinking": "internal"},
                            text_block("Running the tests first.")),
            assistant_event(tool_block("Bash", {"command": "pytest -q"})),
            assistant_event(text_block("Green. Opening the PR.")),
            result_event("Green. Opening the PR."),
        ])

        summarizer.STREAM_LOG_DIR = ags.STREAM_LOG_DIR
        content = summarizer.read_recent_stream("amos")

        assert content, "the summarizer must find and parse the teed stream"
        assert content.splitlines() == [
            "[USER] ship the fix",
            "[TEXT] Running the tests first.",
            "[TOOL] Bash",
            "[TEXT] Green. Opening the PR.",
        ]


# ---------------------------------------------------------------------------
# Defect 2b: the summarizer's own claude call parsed the same wrong shape
# ---------------------------------------------------------------------------

SUMMARY_BODY = (
    "## Primary Task\nFix #148.\n\n"
    "## Current State\nTests are green.\n\n"
    "## Key Context for Next Session\n- Opened the PR\n"
)


class TestCallSummarizer:

    def _fake_run(self, summarizer, monkeypatch, stdout):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd

            class R:
                returncode = 0
            R.stdout = stdout
            R.stderr = ""
            return R

        monkeypatch.setattr(summarizer.subprocess, "run", fake_run)
        return seen

    def test_the_summary_is_read_out_of_assistant_blocks(self, summarizer, monkeypatch):
        """Same defect as the stream parser, one layer up: `claude -p
        --output-format stream-json` answers in assistant blocks too, so the
        old `event["type"] == "text"` loop always produced "" and the run
        failed the required-headers check."""
        self._fake_run(summarizer, monkeypatch,
                       assistant_event(text_block(SUMMARY_BODY)).decode())

        ok, summary, meta = summarizer.call_summarizer("[TEXT] hello")
        assert ok is True, meta
        assert summary.startswith("## Primary Task")

    def test_it_falls_back_to_the_flat_result_string(self, summarizer, monkeypatch):
        self._fake_run(summarizer, monkeypatch, result_event(SUMMARY_BODY).decode())

        ok, summary, meta = summarizer.call_summarizer("[TEXT] hello")
        assert ok is True, meta
        assert "## Current State" in summary

    def test_stream_json_output_is_requested_with_verbose(self, summarizer, monkeypatch):
        """The CLI refuses `--output-format stream-json` under `-p` without
        `--verbose` — it exits non-zero, which the caller reported as the
        opaque "subprocess_failed"."""
        seen = self._fake_run(summarizer, monkeypatch,
                              assistant_event(text_block(SUMMARY_BODY)).decode())
        summarizer.call_summarizer("[TEXT] hello")

        cmd = seen["cmd"]
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--verbose" in cmd

    def test_a_summary_missing_its_headers_is_rejected(self, summarizer, monkeypatch):
        self._fake_run(summarizer, monkeypatch,
                       assistant_event(text_block("just some prose")).decode())

        ok, _, meta = summarizer.call_summarizer("[TEXT] hello")
        assert ok is False
        assert meta["error"] == "missing_headers"


# ---------------------------------------------------------------------------
# Defect 3: the MCP finalize call passed no agent
# ---------------------------------------------------------------------------

@pytest.fixture
def tools_server(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("KARAKOS_AGENT", "amos")
    (tmp_path / "bin").mkdir(parents=True, exist_ok=True)
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


class TestMcpFinalize:
    """Step 2.6: `finalize` means "I want a fresh session" and goes to the
    agent server; it no longer shells out to the summarizer."""

    def _capture(self, tools_server, monkeypatch, status=200, body=None):
        seen = {"calls": []}

        def fake_request(method, path, payload=None, timeout=15.0):
            seen["calls"].append((method, path))
            return status, body if body is not None else {"status": "scheduled"}

        def no_run(*a, **k):
            raise AssertionError("summarize-session.py must not be run")

        monkeypatch.setattr(tools_server, "agent_server_request", fake_request)
        monkeypatch.setattr(tools_server.subprocess, "run", no_run)
        return seen

    def test_finalize_posts_the_callers_shard(self, tools_server, monkeypatch):
        seen = self._capture(tools_server, monkeypatch)
        out = tools_server.handle_core_tool("session", {"action": "finalize"})
        assert out["status"] == "scheduled"
        assert seen["calls"] == [("POST", "/agents/amos/session/finalize")]

    def test_the_shard_wins_over_the_agent_id(self, tools_server, monkeypatch):
        seen = self._capture(tools_server, monkeypatch)
        monkeypatch.setattr(tools_server, "KARAKOS_SHARD", "amos-2")
        tools_server.handle_core_tool("session", {"action": "finalize"})
        assert seen["calls"] == [("POST", "/agents/amos-2/session/finalize")]

    def test_an_explicit_agent_argument_wins(self, tools_server, monkeypatch):
        seen = self._capture(tools_server, monkeypatch)
        tools_server.handle_core_tool("session", {"action": "finalize", "agent": "kara"})
        assert seen["calls"] == [("POST", "/agents/kara/session/finalize")]

    def test_no_identity_is_an_explicit_error(self, tools_server, monkeypatch):
        monkeypatch.setattr(tools_server, "KARAKOS_AGENT", "")
        seen = self._capture(tools_server, monkeypatch)
        out = tools_server.handle_core_tool("session", {"action": "finalize"})
        assert "error" in out and "KARAKOS_AGENT" in out["error"]
        assert seen["calls"] == []

    def test_a_server_error_is_surfaced(self, tools_server, monkeypatch):
        self._capture(tools_server, monkeypatch, status=404, body={"error": "Unknown agent"})
        out = tools_server.handle_core_tool("session", {"action": "finalize"})
        assert out["status"] == "error" and "Unknown agent" in out["error"]

    def test_load_last_returns_this_shards_pending_note(self, tools_server, tmp_path):
        d = tmp_path / "data" / "handoff"
        d.mkdir(parents=True)
        (d / "amos.md").write_text("note for amos")
        (d / "zed.md").write_text("note for zed")
        out = tools_server.handle_core_tool("session", {"action": "load_last"})
        assert out["status"] == "success" and out["summary"] == "note for amos"

    def test_load_last_without_a_note_is_not_found(self, tools_server):
        out = tools_server.handle_core_tool("session", {"action": "load_last"})
        assert out == {"status": "not_found"}

    def test_load_last_explicit_agent_returns_only_that_agents_note(self, tools_server, tmp_path):
        """Fixes #160: load_last must never hand back another agent's summary."""
        d = tmp_path / "data" / "handoff"
        d.mkdir(parents=True)
        (d / "amos.md").write_text("note for amos")
        (d / "zed.md").write_text("note for zed")
        out = tools_server.handle_core_tool("session", {"action": "load_last", "agent": "zed"})
        assert out["summary"] == "note for zed"

    def test_load_last_no_identity_is_an_explicit_error(self, tools_server, monkeypatch, tmp_path):
        monkeypatch.setattr(tools_server, "KARAKOS_AGENT", "")
        monkeypatch.setattr(tools_server, "KARAKOS_SHARD", "")
        d = tmp_path / "data" / "handoff"
        d.mkdir(parents=True)
        (d / "zed.md").write_text("note for zed")
        out = tools_server.handle_core_tool("session", {"action": "load_last"})
        assert "error" in out and "KARAKOS_AGENT" in out["error"]
        assert "summary" not in out

    def test_load_last_rejects_path_traversal(self, tools_server):
        out = tools_server.handle_core_tool("session", {"action": "load_last", "agent": "../x"})
        assert "error" in out

    def test_the_tool_schema_accepts_an_agent(self, tools_server):
        session_tool = next(t for t in tools_server.CORE_TOOLS if t["name"] == "session")
        assert "agent" in session_tool["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# Step 2.6: the handoff note replaces the 24 h [SESSION RESET] summary
# ---------------------------------------------------------------------------

class TestHandoffInjection:

    def _spawn(self, ags, monkeypatch, tmp_path, cleared_at):
        import asyncio as aio

        class Proc:
            pid = 1
            stdin = stdout = None

            class stderr:
                @staticmethod
                async def readline():
                    await aio.sleep(3600)

            async def wait(self):
                await aio.sleep(3600)

        seen = {}

        async def fake_exec(*args, **kw):
            seen["cmd"] = list(args)
            return Proc()

        async def fake_session(shard):
            return "sess-1"

        async def fake_cleared(shard):
            return cleared_at

        monkeypatch.setattr(ags.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(ags, "get_or_create_session", fake_session)
        monkeypatch.setattr(ags, "session_cleared_at", fake_cleared)
        (tmp_path / "agents" / "amos").mkdir(parents=True, exist_ok=True)

        async def go():
            try:
                await ags.start_agent_subprocess("amos")
            finally:
                for reg in (ags.stderr_reader_tasks, ags.respawn_watcher_tasks):
                    for t in reg.values():
                        t.cancel()
                    reg.clear()
        aio.run(go())
        return seen["cmd"]

    def test_a_note_older_than_the_clear_is_injected_once(self, ags, tmp_path, monkeypatch):
        d = tmp_path / "data" / "handoff"
        d.mkdir(parents=True)
        (d / "amos.md").write_text("carry this over")
        old = time.time() - 600
        os.utime(d / "amos.md", (old, old))
        cmd = self._spawn(ags, monkeypatch, tmp_path, "2999-01-01 00:00:00")
        appended = cmd[cmd.index("--append-system-prompt") + 1]
        assert "handoff note from your previous session" in appended
        assert "carry this over" in appended
        assert "[SESSION RESET]" not in appended
        assert not (d / "amos.md").exists()           # rotated, never injected twice
        cmd = self._spawn(ags, monkeypatch, tmp_path, "2999-01-01 00:00:00")
        assert "--append-system-prompt" not in cmd or \
            "carry this over" not in cmd[cmd.index("--append-system-prompt") + 1]

    def test_the_old_summary_file_is_not_injected(self, ags, tmp_path, monkeypatch):
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "last-session-summary-amos.md").write_text("old summary")
        cmd = self._spawn(ags, monkeypatch, tmp_path, "2999-01-01 00:00:00")
        assert "old summary" not in " ".join(cmd)

    def test_no_clear_time_means_no_note_applies(self, ags, tmp_path, monkeypatch):
        d = tmp_path / "data" / "handoff"
        d.mkdir(parents=True)
        (d / "amos.md").write_text("not for this session")
        cmd = self._spawn(ags, monkeypatch, tmp_path, None)
        assert "not for this session" not in " ".join(cmd)
        assert (d / "amos.md").exists()


# ---------------------------------------------------------------------------
# Retention: the tee must not grow without bound
# ---------------------------------------------------------------------------

@pytest.fixture
def purger(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("STREAM_LOG_RETENTION_DAYS", "7")
    (tmp_path / "logs" / "agent-streams").mkdir(parents=True, exist_ok=True)
    return import_script("purge-data")


class TestStreamLogRetention:

    def test_old_stream_logs_are_purged_and_current_ones_kept(self, purger, tmp_path):
        d = tmp_path / "logs" / "agent-streams"
        old = d / "amos_20260101-000000.jsonl"
        current = d / "amos_20260826-120000.jsonl"
        old.write_text("{}\n")
        current.write_text("{}\n")
        stale = time.time() - 30 * 86400
        os.utime(old, (stale, stale))

        assert purger.purge_old_stream_logs() == 1
        assert not old.exists()
        assert current.exists()

    def test_purge_is_by_mtime_not_by_the_name(self, purger, tmp_path):
        """agent-server holds one log open for a whole boot, so the
        timestamp in the name says when it was OPENED. A long-lived agent's
        live log must not be deleted out from under it."""
        d = tmp_path / "logs" / "agent-streams"
        live = d / "amos_20260101-000000.jsonl"
        live.write_text("{}\n")   # opened in January, written to just now

        assert purger.purge_old_stream_logs() == 0
        assert live.exists()

    def test_a_missing_directory_is_not_an_error(self, purger, tmp_path):
        import shutil
        shutil.rmtree(tmp_path / "logs" / "agent-streams")
        assert purger.purge_old_stream_logs() == 0

    def test_main_reports_the_new_bucket(self, purger, monkeypatch):
        """The daily job has to actually call it — a retention function
        nothing invokes is the same shape of bug as the unwritten log."""
        seen = []
        monkeypatch.setattr(purger.log, "info", seen.append)
        purger.main()
        assert any("stream_logs" in str(m) for m in seen), seen
