"""
Tests for #121: a channel waiting behind a busy agent sees nothing until the
drain reaches it.

Before this fix, POST /message only started a typing indicator for the
channel process_agent_queue() happened to be actively draining. A message
that landed in a different channel while the agent was mid-turn elsewhere
got no typing indicator and no ack until that channel's turn came up in a
future drain — indistinguishable, to the human watching, from being ignored.
"""

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

TOKEN = "test-agent-server-token"
CHANNEL_A = "111000111"
CHANNEL_B = "222000222"


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
def workspace(tmp_path):
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def ags(workspace):
    """bin/agent-server.py against a scratch workspace, configured for one agent."""
    module = _load("ags_queued_typing_under_test", AGENT_SERVER, workspace)
    module.AGENT_SERVER_TOKEN = TOKEN
    module.OWNER_DISCORD_ID = "9001"
    module.agent_config = {"amos": {"model": "sonnet"}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    return module


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
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


class FakeDiscordREST:
    """Records requests made to the Discord API. Typing posts return 200
    (not 204), which is deliberate: start_typing()'s loop treats any
    non-204 response as "stop retrying", so a single capture is enough to
    prove the indicator fired without the test having to manage a
    long-lived retry loop."""

    def __init__(self, status=200):
        self.status = status
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, "json": kwargs.get("json"),
                           "headers": kwargs.get("headers", {})})
        return FakeResponse(self.status, {"id": f"discord-msg-{len(self.posts)}"})

    @property
    def typing_urls(self):
        return [p["url"] for p in self.posts if p["url"].endswith("/typing")]


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


async def wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


def run(ags, coro_fn):
    """Run a scenario, tearing the module's global state down unconditionally.

    aiosqlite.Connection runs a NON-DAEMON worker thread that stops only on
    .close() — __del__ would do it, but sys.modules holding the
    freshly-exec'd module alive means __del__ never fires. Closing it at the
    end of the scenario body therefore works only while the scenario
    passes: a failed assertion skips the close and the interpreter hangs at
    shutdown instead of reporting the failure.

    That is not a hypothetical. Mutating the multi-channel stop_typing() back
    to the single-channel form made this file hang rather than fail, which
    would read in CI as a wedged job rather than a caught regression — and
    the whole point of these tests is to be legible when they go red.
    """

    async def wrapper():
        try:
            await coro_fn()
        finally:
            for task in list(ags.typing_tasks.values()):
                task.cancel()
            ags.typing_tasks.clear()
            db = getattr(ags, "db", None)
            if db is not None:
                await db.close()
                ags.db = None

    asyncio.run(wrapper())


def test_message_queued_behind_a_busy_turn_gets_typing_in_its_own_channel(ags):
    """The acceptance test from #121: channel B shows typing within 5s while
    the agent is still mid-turn in channel A."""

    async def scenario():
        ags.http_session = FakeDiscordREST()
        server = TestServer(ags.create_app(with_lifecycle=False))
        client = TestClient(server)
        await client.start_server()

        await ags.init_db()
        ags.agent_locks["amos"] = asyncio.Lock()
        # Agent is mid-turn in channel A.
        ags.agent_states["amos"] = "PROCESSING"

        resp = await client.post(
            "/message",
            json={
                "agent": "amos", "channel": "b", "channel_id": CHANNEL_B,
                "server": "local", "author": "Mike", "author_id": "42",
                "content": "hello while you're busy", "message_id": "msg-b1",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 202, await resp.text()

        await wait_until(lambda: bool(ags.http_session.typing_urls))
        assert any(CHANNEL_B in url for url in ags.http_session.typing_urls)

        await client.close()

    run(ags, scenario)


def test_batch_spanning_two_channels_stops_typing_for_both(ags):
    """A batch holds one channel only (claim_batch), so a message waiting in a
    second channel is not in this turn: its typing indicator is left running
    for the turn it will get, and the turn's own channel is cleared."""

    async def scenario():
        ags.http_session = FakeDiscordREST()
        await ags.init_db()
        ags.agent_locks["amos"] = asyncio.Lock()
        ags.agent_states["amos"] = "IDLE"

        for channel_id, msg_id in ((CHANNEL_A, "msg-a1"), (CHANNEL_B, "msg-b1")):
            await ags.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
                " author_id, is_bot, content, message_id, mentions_agent)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("amos", "c", channel_id, "discord", "Mike", "42", 0,
                 "hi", msg_id, 1),
            )
        await ags.db.commit()

        # Simulate what handle_message's elif already did for the
        # second-arriving channel before the drain picked up both messages.
        await ags.start_typing("amos", CHANNEL_B)
        assert CHANNEL_B in ags.typing_tasks

        result_event = json.dumps({
            "type": "result", "session_id": "s1", "result": "done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode() + b"\n"
        ags.agent_processes["amos"] = FakeProc(FakeStdout([result_event]))

        await ags.process_agent_queue("amos")

        # One batch is one channel: A's turn ran and cleared A. B's row is still
        # queued for its own turn, so its indicator (an honest "a turn is coming")
        # stays until that turn ends.
        assert CHANNEL_A not in ags.typing_tasks
        assert CHANNEL_B in ags.typing_tasks
        async with ags.db.execute(
            "SELECT processed FROM message_queue WHERE message_id = 'msg-b1'"
        ) as cur:
            assert (await cur.fetchone())["processed"] == ags.STATUS_QUEUED

    run(ags, scenario)


@pytest.mark.parametrize("state", ["ERROR_RECOVERY", None])
def test_no_typing_when_no_turn_is_actually_running(ags, state):
    """The indicator is a promise that a turn is in flight and will end.

    Only process_agent_queue() calls stop_typing(), and it only runs on the
    IDLE branch of handle_message. In ERROR_RECOVERY — or for an agent that
    never started, which has no state at all — no turn is running to clear
    the indicator, so firing one would leave it spinning until the process
    restarts. Queue the message, say nothing.
    """

    async def scenario():
        ags.http_session = FakeDiscordREST()
        server = TestServer(ags.create_app(with_lifecycle=False))
        client = TestClient(server)
        await client.start_server()

        await ags.init_db()
        ags.agent_locks["amos"] = asyncio.Lock()
        ags.agent_states.pop("amos", None)
        if state is not None:
            ags.agent_states["amos"] = state

        resp = await client.post(
            "/message",
            json={
                "agent": "amos", "channel": "b", "channel_id": CHANNEL_B,
                "server": "local", "author": "Mike", "author_id": "42",
                "content": "anyone home", "message_id": "msg-b1",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 202, await resp.text()

        # The message is queued either way — this is about the indicator.
        async with ags.db.execute(
            "SELECT COUNT(*) AS count FROM message_queue WHERE processed = ?",
            (ags.STATUS_QUEUED,),
        ) as cursor:
            assert (await cursor.fetchone())["count"] == 1

        # Give a create_task'd start_typing every chance to have run.
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

        assert ags.http_session.typing_urls == []
        assert CHANNEL_B not in ags.typing_tasks

        await client.close()

    run(ags, scenario)


def test_message_arriving_mid_turn_is_drained_when_that_turn_ends(ags):
    """The other half of #121, and the reason the indicator can be honest.

    handle_message is the only caller of process_agent_queue() and it fires
    only on the IDLE branch, so before this a message that landed mid-turn
    waited for the *next* inbound message to sweep it up, not for the turn
    it was queued behind to finish. Nothing in this test posts a second
    message: the only way channel B's message reaches STATUS_COMPLETE is the
    re-drain at the end of the turn.
    """

    async def scenario():
        ags.http_session = FakeDiscordREST()
        await ags.init_db()
        ags.agent_locks["amos"] = asyncio.Lock()
        ags.agent_states["amos"] = "IDLE"

        await ags.db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
            " author_id, is_bot, content, message_id, mentions_agent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("amos", "a", CHANNEL_A, "discord", "Mike", "42", 0, "first", "msg-a1", 1),
        )
        await ags.db.commit()

        def result_line(text):
            return json.dumps({
                "type": "result", "session_id": "s1", "result": text,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode() + b"\n"

        class InjectingStdout(FakeStdout):
            """Channel B messages the agent while the channel A turn is
            still being read — the exact race #121 describes."""

            def __init__(self, lines):
                super().__init__(lines)
                self.injected = False

            async def readline(self):
                if not self.injected:
                    self.injected = True
                    await ags.db.execute(
                        "INSERT INTO message_queue (agent, channel, channel_id, server,"
                        " author, author_id, is_bot, content, message_id, mentions_agent)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ("amos", "b", CHANNEL_B, "discord", "Mike", "42", 0,
                         "second", "msg-b1", 1),
                    )
                    await ags.db.commit()
                return await super().readline()

        stdout = InjectingStdout([result_line("reply to A"), result_line("reply to B")])
        ags.agent_processes["amos"] = FakeProc(stdout)

        await ags.process_agent_queue("amos")

        async def b_is_done():
            async with ags.db.execute(
                "SELECT processed FROM message_queue WHERE message_id = ?", ("msg-b1",)
            ) as cursor:
                row = await cursor.fetchone()
            return row is not None and row["processed"] == ags.STATUS_COMPLETE

        # wait_until() takes a sync predicate; this one has to query.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if await b_is_done():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("msg-b1 was never drained after the turn ended")

        # And it was answered in its own channel, not appended to A's.
        posted = [p for p in ags.http_session.posts if not p["url"].endswith("/typing")]
        assert any(CHANNEL_B in p["url"] for p in posted), posted

        assert CHANNEL_B not in ags.typing_tasks
    run(ags, scenario)


def test_typing_is_cleared_for_every_batch_channel_when_the_turn_raises(ags):
    """read_agent_response raising is the one path where nothing downstream
    clears the indicators, and #121 turned that from one stuck channel into
    one per channel in the batch. The stop lives in the existing finally for
    exactly this case."""

    async def scenario():
        ags.http_session = FakeDiscordREST()
        await ags.init_db()
        ags.agent_locks["amos"] = asyncio.Lock()
        ags.agent_states["amos"] = "IDLE"

        for channel_id, msg_id in ((CHANNEL_A, "msg-a1"), (CHANNEL_B, "msg-b1")):
            await ags.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
                " author_id, is_bot, content, message_id, mentions_agent)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("amos", "c", channel_id, "discord", "Mike", "42", 0, "hi", msg_id, 1),
            )
        await ags.db.commit()

        await ags.start_typing("amos", CHANNEL_B)
        assert CHANNEL_B in ags.typing_tasks

        async def boom(*args, **kwargs):
            raise RuntimeError("subprocess died mid-turn")

        ags.read_agent_response = boom
        ags.agent_processes["amos"] = FakeProc(FakeStdout([]))

        with pytest.raises(RuntimeError):
            await ags.process_agent_queue("amos")

        # One batch is one channel: A's turn ran and cleared A. B's row is still
        # queued for its own turn, so its indicator (an honest "a turn is coming")
        # stays until that turn ends.
        assert CHANNEL_A not in ags.typing_tasks
        assert CHANNEL_B in ags.typing_tasks
        async with ags.db.execute(
            "SELECT processed FROM message_queue WHERE message_id = 'msg-b1'"
        ) as cur:
            assert (await cur.fetchone())["processed"] == ags.STATUS_QUEUED

    run(ags, scenario)
