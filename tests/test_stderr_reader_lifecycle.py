"""
Regression test for #108: asyncio.create_task(stderr_reader(...)) was fired
bare and unreferenced in start_agent_subprocess(). asyncio holds only a weak
reference to an untracked task, so it can be garbage-collected mid-flight,
and stale readers from prior subprocesses were never cancelled on kill or
respawn.

The fix tracks each agent's reader task in `stderr_reader_tasks` and cancels
the stale one on both kill and respawn. These tests exercise that lifecycle
directly against start_agent_subprocess/kill_agent_subprocess with a fake
subprocess, without booting the full aiohttp server.
"""

import asyncio

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    workspace = tmp_path
    (workspace / "agents" / "testagent" / "persona").mkdir(parents=True)
    (workspace / "config").mkdir()
    (workspace / "system_prompt.md").write_text("You are a test agent.")

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    mod = import_script("agent-server")
    mod.WORKSPACE_ROOT = workspace
    mod.agent_config["testagent"] = {
        "system_prompt": "system_prompt.md",
        "model": "sonnet",
        "max_turns": 5,
    }

    async def fake_get_or_create_session(agent):
        return "session-1234"

    async def fake_load_last_session(agent):
        return {"status": "not_found"}

    monkeypatch.setattr(mod, "get_or_create_session", fake_get_or_create_session)
    monkeypatch.setattr(mod, "load_last_session", fake_load_last_session)

    yield mod

    # Belt-and-suspenders cleanup: cancel anything left running so a failing
    # assertion doesn't leak a live task into the next test's event loop.
    for task in mod.stderr_reader_tasks.values():
        if not task.done():
            task.cancel()


class FakeStderr:
    """Stands in for proc.stderr: readline() blocks until cancelled, like a
    real pipe on a subprocess that hasn't exited."""

    async def readline(self):
        await asyncio.sleep(3600)
        return b""  # pragma: no cover - never reached in tests


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.stderr = FakeStderr()
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


def _patch_subprocess(mod, monkeypatch, processes):
    """processes: iterable of FakeProcess to hand out on each call."""
    it = iter(processes)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return next(it)

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


def test_start_tracks_stderr_reader_task(agent_server, monkeypatch):
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1)])

    async def scenario():
        await mod.start_agent_subprocess("testagent")

        task = mod.stderr_reader_tasks.get("testagent")
        assert task is not None
        assert not task.done()

    asyncio.run(scenario())


def test_respawn_cancels_stale_stderr_reader(agent_server, monkeypatch):
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1), FakeProcess(pid=2)])

    async def scenario():
        await mod.start_agent_subprocess("testagent")
        first_task = mod.stderr_reader_tasks["testagent"]
        assert not first_task.done()

        await mod.start_agent_subprocess("testagent")
        second_task = mod.stderr_reader_tasks["testagent"]

        assert second_task is not first_task
        await asyncio.sleep(0)  # let the cancellation propagate
        assert first_task.cancelled()
        assert not second_task.done()

    asyncio.run(scenario())


def test_kill_cancels_stderr_reader_and_clears_tracking(agent_server, monkeypatch):
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1)])

    async def scenario():
        await mod.start_agent_subprocess("testagent")
        task = mod.stderr_reader_tasks["testagent"]
        assert not task.done()

        await mod.kill_agent_subprocess("testagent")
        await asyncio.sleep(0)

        assert task.cancelled()
        assert "testagent" not in mod.stderr_reader_tasks
        assert "testagent" not in mod.agent_processes

    asyncio.run(scenario())


def test_fifty_respawns_leave_flat_task_count(agent_server, monkeypatch):
    """Acceptance test from #108: force 50 respawns, task count stays flat
    (only the live reader for the current subprocess remains tracked)."""
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=n) for n in range(1, 51)])

    async def scenario():
        for _ in range(50):
            await mod.start_agent_subprocess("testagent")

        await asyncio.sleep(0)

        assert len(mod.stderr_reader_tasks) == 1
        live = [t for t in mod.stderr_reader_tasks.values() if not t.done()]
        assert len(live) == 1

    asyncio.run(scenario())
