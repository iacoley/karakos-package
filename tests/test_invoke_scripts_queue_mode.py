"""KARAKOS_QUEUE_RUN guard in the invoke scripts, and "off by default is identical"
for the relay's DispatchAdapter (spec 3.3)."""
import asyncio
import importlib.util
import inspect
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildq_helpers as bq  # noqa: E402
from buildq_helpers import ROOT, make_workspace, write_script  # noqa: E402

GH_PR = "https://github.com/owner/name/pull/42"
GUARD = ('# Queue-owned run (spec 3.3): the dispatcher notifies the requester and owns the row.\n'
         'if [[ -n "${KARAKOS_QUEUE_RUN:-}" ]]; then\n    exit $EXIT_CODE\nfi\n\n')


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = make_workspace(tmp_path)
    bq.isolate(monkeypatch, tmp_path, w)
    monkeypatch.setenv("AGENT_SERVER_PORT", "9")        # nothing listens: the cost post fails fast
    return w


def old_script(name, tmp_path):
    """The script as it was before 3.3: the new one without the guard block."""
    text = (ROOT / "bin" / name).read_text()
    assert text.count(GUARD) == 1 and text.count("KARAKOS_QUEUE_RUN") == 1
    d = tmp_path / "old" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    (d.parent / "lib").symlink_to(ROOT / "lib")
    if not (d / "lib").exists():
        (d / "lib").symlink_to(ROOT / "bin" / "lib")
    p = d / name
    p.write_text(text.replace(GUARD, ""))
    p.chmod(0o755)
    return p


def run_script(script, ws, brief_path, queue=False, env_extra=None):
    env = dict(os.environ, WORKSPACE_ROOT=str(ws))
    env.pop("KARAKOS_QUEUE_RUN", None)
    if queue:
        env["KARAKOS_QUEUE_RUN"] = "1"
    env.update(env_extra or {})
    for f in (ws / "poke.log", bq.Path(os.environ["FAKE_CLAUDE_LOG_DIR"]) / "prompts.jsonl"):
        if f.exists():
            f.unlink()
    p = subprocess.run([str(script), str(brief_path)], capture_output=True, text=True, env=env,
                       cwd=str(ws), stdin=subprocess.DEVNULL, timeout=60)
    prompts = Path(os.environ["FAKE_CLAUDE_LOG_DIR"]) / "prompts.jsonl"
    argv = [json.loads(l)["argv"] for l in prompts.read_text().splitlines()] if prompts.exists() else []
    return p, bq.pokes(ws), argv


def new_brief(ws, name="job.md"):
    f = ws / "inbox" / "builder" / name
    f.write_text(bq.brief())
    return f


def strip_ids(argv):
    return [[a for a in call] for call in argv]


@pytest.mark.parametrize("name,agent", [("invoke-builder.sh", "bld"), ("invoke-reviewer.sh", "rev")])
def test_unset_is_identical_to_the_old_script(ws, tmp_path, name, agent):
    inbox = ws / "inbox" / ("builder" if name == "invoke-builder.sh" else "reviewer")
    write_script(tmp_path / "script.json", {"text": f"Done. {GH_PR} Verdict: APPROVE"})
    f1 = inbox / "one.md"
    f1.write_text(bq.brief(body="same body"))
    old = old_script(name, tmp_path)
    shim = ws / "bin" / name
    old_p, old_pokes, old_argv = run_script(old, ws, f1)
    # restore the brief the old script archived, run the new one the same way
    arch = list((inbox.parent / agent / "archive").glob("*one.md")) if (inbox.parent / agent).exists() else []
    assert len(arch) == 1, "the old script archives the brief"
    arch[0].rename(f1)
    new_p, new_pokes, new_argv = run_script(shim, ws, f1)
    assert new_p.returncode == old_p.returncode == 0
    assert old_pokes == new_pokes and len(new_pokes) == 1
    assert old_argv == new_argv and len(new_argv) == 1
    assert len(list((inbox.parent / agent / "archive").glob("*one.md"))) == 1       # archived again
    assert not f1.exists()


@pytest.mark.parametrize("name,agent", [("invoke-builder.sh", "bld"), ("invoke-reviewer.sh", "rev")])
def test_queue_mode_skips_poke_and_archive(ws, tmp_path, name, agent):
    inbox = ws / "inbox" / ("builder" if name == "invoke-builder.sh" else "reviewer")
    write_script(tmp_path / "script.json", {"text": f"Done. {GH_PR}"})
    f1 = inbox / "q.md"
    f1.write_text(bq.brief())
    p, pokes, argv = run_script(ws / "bin" / name, ws, f1, queue=True)
    assert p.returncode == 0 and pokes == [] and len(argv) == 1
    assert f1.exists()                                           # the dispatcher owns the file/row
    assert not (inbox.parent / agent / "archive").exists()


def test_queue_mode_still_propagates_the_exit_code(ws, tmp_path):
    write_script(tmp_path / "script.json", {"exit": 7})
    f1 = new_brief(ws)
    p, pokes, _ = run_script(ws / "bin" / "invoke-builder.sh", ws, f1, queue=True)
    assert p.returncode == 7 and pokes == []


def test_inbox_path_does_not_notify_on_a_failed_run_set_e(ws, tmp_path):
    """Spec 3.3 'Why': under `set -euo pipefail` a non-zero `claude` ends the script at the
    pipeline, so the 'Build finished (exit code ...)' notice and the archive step never
    run. This pins what the INBOX path does today (it is deliberately not changed here):
    the claim is true, which is why the queue dispatcher owns notification."""
    write_script(tmp_path / "script.json", {"exit": 1})
    f1 = new_brief(ws)
    p, pokes, _ = run_script(ws / "bin" / "invoke-builder.sh", ws, f1)
    assert p.returncode == 1
    assert pokes == []
    assert f1.exists()                                           # never archived


# --- the relay: off by default ---------------------------------------------------------------------

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")
RELAY_PATH = ROOT / "bin" / "relay.py"


@pytest.fixture
def relay(tmp_path, monkeypatch):
    w = make_workspace(tmp_path)
    (w / "config" / "channels.json").write_text(json.dumps({"channels": {}}))
    monkeypatch.setenv("WORKSPACE_ROOT", str(w))
    for k in ("DISCORD_BOT_TOKEN_PRIM", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("relay_dispatch_under_test", RELAY_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_dispatch_under_test"] = mod
    spec.loader.exec_module(mod)
    mod.ws = w
    return mod


def run_main(relay, monkeypatch):
    """Run relay.main() with both dispatchers replaced by recorders (no token: it
    takes the dispatch-only loop); cancel once start() was recorded."""
    calls = []

    class RecAdapter:
        def __init__(self):
            calls.append("DispatchAdapter")

        async def start(self):
            calls.append("adapter.start")

        async def stop(self):
            calls.append("adapter.stop")

    class RecQueue:
        def open(self):
            calls.append("queue.open")

        async def start(self):
            calls.append("queue.start")

        async def stop(self):
            calls.append("queue.stop")

    monkeypatch.setattr(relay, "DispatchAdapter", RecAdapter)
    monkeypatch.setattr(relay.build_dispatcher, "build_dispatcher",
                        lambda *a, **k: (calls.append("QueueDispatcher"), RecQueue())[1])

    async def go():
        t = asyncio.ensure_future(relay.main())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any(c.endswith(".start") for c in calls) or len(calls) >= 1 and t.done():
                break
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
    asyncio.run(go())
    return calls


def test_dispatch_adapter_source_is_byte_identical_to_release_2_0(relay):
    golden = (ROOT / "tests" / "golden" / "dispatch_adapter.py.txt").read_text()
    assert inspect.getsource(relay.DispatchAdapter) == golden


def test_shipped_default_constructs_the_old_adapter_only(relay, monkeypatch):
    shipped = ROOT / "lib" / "migrate" / "steps" / "50_build_queue.py"
    spec = importlib.util.spec_from_file_location("step50", shipped)
    # the config the migrator writes is `enabled: false`
    sys.path.insert(0, str(ROOT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (relay.ws / "config" / "build-queue.yaml").write_text(mod.CONFIG_TEXT)
    calls = run_main(relay, monkeypatch)
    assert "DispatchAdapter" in calls and "QueueDispatcher" not in calls


def test_no_config_file_constructs_the_old_adapter_only(relay, monkeypatch):
    assert not (relay.ws / "config" / "build-queue.yaml").exists()
    calls = run_main(relay, monkeypatch)
    assert "DispatchAdapter" in calls and "QueueDispatcher" not in calls


def test_enabled_constructs_the_queue_dispatcher_only(relay, monkeypatch):
    (relay.ws / "config" / "build-queue.yaml").write_text("enabled: true\n")
    calls = run_main(relay, monkeypatch)
    assert "QueueDispatcher" in calls and "queue.start" in calls and "DispatchAdapter" not in calls


def test_enabled_with_an_invalid_file_runs_neither_and_logs_an_error(relay, monkeypatch, caplog):
    (relay.ws / "config" / "build-queue.yaml").write_text("enabled: true\ndefault_host: ghost\n")
    with caplog.at_level(logging.ERROR, logger="relay"):
        relay.log.propagate = True
        calls = run_main(relay, monkeypatch)
    assert "DispatchAdapter" not in calls and "QueueDispatcher" not in calls
    assert any("build queue config invalid" in r.getMessage() for r in caplog.records)


def test_disabled_but_invalid_file_keeps_the_old_adapter(relay, monkeypatch):
    (relay.ws / "config" / "build-queue.yaml").write_text("enabled: false\ndefault_host: ghost\n")
    calls = run_main(relay, monkeypatch)
    assert "DispatchAdapter" in calls and "QueueDispatcher" not in calls
