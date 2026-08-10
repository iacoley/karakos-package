"""
Tests for the operational agent-server routes behind the Discord slash
commands — issue #86.

`/interrupt`, `/kill` and `/flush` are only as real as the endpoints they
POST to, so these drive the actual aiohttp route table built by
`create_app()` over real HTTP, against a real sqlite database. A test that
called `interrupt_agent()` directly would pass with the route unwired.

The `/interrupt` test in particular runs a real subprocess that behaves like
a wedged generation — it streams assistant events forever and never emits a
`result` — and asserts that the turn unwinds after the interrupt. There is no
`sleep()` anywhere: the deadline is on `asyncio.wait_for`, so the test fails
if the turn does not end rather than passing because the sleep was long
enough.
"""

import asyncio
import importlib.util
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER_PATH = PACKAGE_ROOT / "bin" / "agent-server.py"

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("aiosqlite")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Anything the /interrupt acceptance test is allowed to take. #86 says the
# generation has to stop within 5 seconds of the command.
INTERRUPT_DEADLINE_SEC = 5.0


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import bin/agent-server.py against a temp workspace."""
    workspace = tmp_path_factory.mktemp("agent-server-workspace")
    for d in ("logs/agent-streams", "data/memory", "data/health/agents"):
        (workspace / d).mkdir(parents=True, exist_ok=True)

    prev = {k: os.environ.get(k) for k in ("WORKSPACE_ROOT", "AGENT_SERVER_TOKEN")}
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    os.environ["AGENT_SERVER_TOKEN"] = TOKEN
    try:
        spec = importlib.util.spec_from_file_location(
            "agent_server_ops_under_test", AGENT_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_server_ops_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


async def _client(server):
    """A live HTTP client bound to the real route table."""
    app = server.create_app(with_lifecycle=False)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _fresh_db(server, tmp_path):
    """A real sqlite database on the module's `db` global.

    aiosqlite runs its connection on a non-daemon thread, so every test that
    opens one must close it inside the same event loop — an unclosed handle
    hangs the interpreter at exit, and a suite that never exits looks exactly
    like a suite that never finishes.
    """
    import aiosqlite
    server.DB_PATH = tmp_path / "agent-server.db"
    server.db = await aiosqlite.connect(str(server.DB_PATH))
    server.db.row_factory = aiosqlite.Row
    await server.init_db()
    return server.db


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Auth and unknown agents — same shape on all three new routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["interrupt", "kill", "flush"])
def test_route_requires_the_bearer_token(server, route, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        client = await _client(server)
        try:
            resp = await client.post(f"/agents/amos/{route}")
            return resp.status
        finally:
            await client.close()

    assert run(go()) == 401


@pytest.mark.parametrize("route", ["interrupt", "kill", "flush"])
def test_route_rejects_an_unknown_agent(server, route, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        client = await _client(server)
        try:
            resp = await client.post(f"/agents/nobody/{route}", headers=AUTH)
            return resp.status
        finally:
            await client.close()

    assert run(go()) == 404


# ---------------------------------------------------------------------------
# /flush
# ---------------------------------------------------------------------------

def test_flush_drops_queued_messages_and_reports_the_count(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_config", {"amos": {}, "kothar": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        for i in range(3):
            await db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
                " author_id, is_bot, content, message_id) VALUES (?,?,?,?,?,?,?,?,?)",
                ("amos", "general", "1", "discord", "mike", "2", 0, f"hi {i}", f"m{i}"))
        # Another agent's backlog must survive.
        await db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
            " author_id, is_bot, content, message_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("kothar", "general", "1", "discord", "mike", "2", 0, "keep me", "k0"))
        await db.commit()

        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/flush", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()

        async with db.execute(
            "SELECT agent, COUNT(*) c FROM message_queue WHERE processed = ? GROUP BY agent",
            (server.STATUS_QUEUED,)
        ) as cur:
            remaining = {r["agent"]: r["c"] for r in await cur.fetchall()}
        await db.close()
        return body, remaining

    body, remaining = run(go())
    assert body["flushed"] == 3
    assert "amos" not in remaining, "amos still has queued messages after a flush"
    assert remaining.get("kothar") == 1, "flush hit another agent's queue"


def test_flush_on_an_empty_queue_reports_zero(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/flush", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()
            await db.close()
        return body

    assert run(go())["flushed"] == 0


# ---------------------------------------------------------------------------
# /kill
# ---------------------------------------------------------------------------

def test_kill_ends_a_real_subprocess_and_does_not_respawn_it(server, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})

    respawns = []

    async def no_respawn(agent):
        respawns.append(agent)

    monkeypatch.setattr(server, "start_agent_subprocess", no_respawn)

    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        server.agent_processes["amos"] = proc

        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/kill", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()
        return body, proc.returncode

    body, returncode = run(go())
    assert body["was_running"] is True
    assert returncode is not None, "subprocess is still alive after /kill"
    assert respawns == [], "/kill respawned the agent — that is /reload"
    assert "amos" not in server.agent_processes


def test_kill_says_so_when_nothing_was_running(server, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})

    async def go():
        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/kill", headers=AUTH)
            return await resp.json()
        finally:
            await client.close()

    assert run(go())["was_running"] is False


# ---------------------------------------------------------------------------
# /interrupt — the acceptance test from #86
# ---------------------------------------------------------------------------

# Streams assistant text forever and never emits a `result`, which is exactly
# what a runaway generation looks like to read_agent_response.
NEVER_ENDING_AGENT = textwrap.dedent("""
    import json, sys, time
    sys.stdin.readline()
    i = 0
    while True:
        i += 1
        sys.stdout.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "tick %d " % i}]},
        }) + "\\n")
        sys.stdout.flush()
        time.sleep(0.02)
""")


def _spawn_runaway():
    return asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", NEVER_ENDING_AGENT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


def test_interrupt_stops_a_running_generation_and_discards_the_partial(
        server, monkeypatch, tmp_path):
    """#86's sharp case. A generation is under way; /interrupt arrives over
    HTTP; the turn must unwind inside the deadline, and the half-written reply
    must not be returned for posting."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})
    monkeypatch.setattr(server, "agent_states", {})
    monkeypatch.setattr(server, "interrupted_agents", set())

    respawned = []

    async def fake_start(agent):
        respawned.append(agent)

    monkeypatch.setattr(server, "start_agent_subprocess", fake_start)

    async def go():
        await _fresh_db(server, tmp_path)
        proc = await _spawn_runaway()
        server.agent_processes["amos"] = proc
        server.agent_states["amos"] = "PROCESSING"
        proc.stdin.write(b"go\n")
        await proc.stdin.drain()

        turn = asyncio.create_task(
            server.read_agent_response("amos", "0", []))

        # Wait for the generation to actually be producing output before
        # interrupting — an interrupt that races the first token would prove
        # nothing about stopping one in flight.
        deadline = asyncio.get_event_loop().time() + 5
        while not server.response_buffers.get("amos"):
            assert not turn.done(), "turn ended before it produced anything"
            assert asyncio.get_event_loop().time() < deadline, "agent never streamed"
            await asyncio.sleep(0.01)

        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/interrupt", headers=AUTH)
            body = await resp.json()
            # The turn must end on its own once the interrupt lands. The
            # deadline is the assertion; nothing here sleeps for a fixed time.
            text, metadata = await asyncio.wait_for(turn, timeout=INTERRUPT_DEADLINE_SEC)
        finally:
            await client.close()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await server.db.close()
        return body, text, metadata, server.agent_states.get("amos")

    body, text, metadata, state = run(go())

    assert body["interrupted"] is True
    assert text == "", f"the abandoned partial reply was returned for posting: {text!r}"
    assert metadata == {}
    assert state == "IDLE", "agent left un-idle — the next message would never be picked up"
    assert respawned == ["amos"], "session was not resumed after the interrupt"


def test_interrupt_says_nothing_was_running_when_the_agent_is_idle(server, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_states", {"amos": "IDLE"})
    monkeypatch.setattr(server, "agent_processes", {})

    killed = []

    async def record_kill(agent):
        killed.append(agent)

    monkeypatch.setattr(server, "kill_agent_subprocess", record_kill)

    async def go():
        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/interrupt", headers=AUTH)
            return resp.status, await resp.json()
        finally:
            await client.close()

    status, body = run(go())
    assert status == 200
    assert body["interrupted"] is False
    assert killed == [], "an idle agent's subprocess was killed by /interrupt"


def test_a_normal_turn_is_not_discarded(server, monkeypatch, tmp_path):
    """The counterweight to the discard: with no interrupt in flight,
    read_agent_response must still hand back what the agent said."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})
    monkeypatch.setattr(server, "agent_states", {})
    monkeypatch.setattr(server, "interrupted_agents", set())

    script = textwrap.dedent("""
        import json, sys
        sys.stdin.readline()
        sys.stdout.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "the answer"}]},
        }) + "\\n")
        sys.stdout.write(json.dumps({"type": "result", "result": "the answer"}) + "\\n")
        sys.stdout.flush()
    """)

    async def go():
        await _fresh_db(server, tmp_path)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        server.agent_processes["amos"] = proc
        server.agent_states["amos"] = "PROCESSING"
        proc.stdin.write(b"go\n")
        await proc.stdin.drain()
        try:
            text, metadata = await asyncio.wait_for(
                server.read_agent_response("amos", "0", []), timeout=10)
            await proc.wait()
        finally:
            await server.db.close()
        return text, metadata

    text, metadata = run(go())
    assert "the answer" in text
    assert metadata != {}
