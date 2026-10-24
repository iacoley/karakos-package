"""
karakos-127: a resumed spawn the CLI refuses used to crash-loop. Each respawn
passed the same --resume id, the process died at once, and the crashloop brake
left the agent down after the fourth exit.

After RESUME_FAIL_GIVEUP fast deaths in a row of a resumed spawn, respawn_watcher
calls clear_session() so the next spawn starts a fresh session. These drive
respawn_watcher with fake processes, the same way test_respawn_notice.py does.
"""

import asyncio
import logging
import time

from test_respawn_notice import agent_server, posted, FakeProcess, _patch_subprocess, _settle  # noqa: F401


def _setup(mod, monkeypatch, processes, *, resumed=True):
    """Fresh per-test resume state, stubbed session lookups, and a list that
    records every clear_session() call."""
    mod.resume_spawn_info.clear()
    mod.resume_fail_streak.clear()
    cleared = []

    async def fake_session_started(agent):
        return resumed

    async def fake_clear_session(agent):
        cleared.append(agent)

    monkeypatch.setattr(mod, "session_started", fake_session_started)
    monkeypatch.setattr(mod, "clear_session", fake_clear_session)
    _patch_subprocess(mod, monkeypatch, processes)
    return cleared


def test_three_fast_resume_deaths_clear_the_session(agent_server, monkeypatch, posted):
    mod = agent_server
    procs = [FakeProcess(pid=i) for i in range(1, 5)]
    cleared = _setup(mod, monkeypatch, procs)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        for proc in procs[:3]:
            proc.exit(1)
            await _settle()

        assert cleared == ["testagent"]
        assert mod.agent_processes["testagent"] is procs[3]

    asyncio.run(scenario())


def test_two_fast_deaths_do_not_clear(agent_server, monkeypatch, posted):
    mod = agent_server
    procs = [FakeProcess(pid=i) for i in range(1, 4)]
    cleared = _setup(mod, monkeypatch, procs)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        for proc in procs[:2]:
            proc.exit(1)
            await _settle()

        assert cleared == []

    asyncio.run(scenario())


def test_slow_death_resets_the_streak(agent_server, monkeypatch, posted):
    mod = agent_server
    procs = [FakeProcess(pid=i) for i in range(1, 5)]
    cleared = _setup(mod, monkeypatch, procs)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        for proc in procs[:3]:
            # Each resumed spawn lived 500s: not a refusal, so the streak restarts.
            mod.resume_spawn_info["testagent"] = ("session-1234", time.monotonic() - 500)
            proc.exit(1)
            await _settle()

        assert cleared == []

    asyncio.run(scenario())


def test_non_resume_spawn_never_counts(agent_server, monkeypatch, posted):
    mod = agent_server
    procs = [FakeProcess(pid=i) for i in range(1, 5)]
    cleared = _setup(mod, monkeypatch, procs, resumed=False)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        for proc in procs[:3]:
            proc.exit(1)
            await _settle()

        assert cleared == []

    asyncio.run(scenario())


def test_fallback_logs_the_named_line(agent_server, monkeypatch, posted, caplog):
    mod = agent_server
    procs = [FakeProcess(pid=i) for i in range(1, 5)]
    _setup(mod, monkeypatch, procs)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        with caplog.at_level(logging.ERROR):
            for proc in procs[:3]:
                proc.exit(1)
                await _settle()

    asyncio.run(scenario())

    assert "resume of session-1234 refused 3x - starting a fresh session" in caplog.text
