"""
Tests for issue #98 — re-inject a recall block before every user message.

Today memory only enters a session once, at spawn, via --append-system-prompt
(bin/agent-server.py). A week-old session answers from whatever was true
when it started. system/hooks/inject-recall.py fixes that by re-injecting a
recall block through Claude Code's UserPromptSubmit hook, on every message,
with a skip gate for automated traffic (system pokes / heartbeats /
task-complete notifications from bin/poke.sh — never a human).

Every test below invokes the real hook script as a subprocess with the
exact JSON payload Claude Code sends on UserPromptSubmit — never a mock or
a source-text grep, per the PR #114 review that killed two weak tests
exactly that way (see test_hooks_wiring.py's module docstring).

Real end-to-end proof this actually reaches the model (verified by hand,
not asserted here since it needs the live `claude` CLI + credentials):

    $ WORKSPACE_ROOT=/tmp/e2erecall claude -p \\
        --settings config/claude-settings.json \\
        --dangerously-skip-permissions \\
        "What is today's secret codeword?"
    Today's secret codeword is PINEAPPLE-42.

with config/recall-source containing "The secret codeword for today is
PINEAPPLE-42." — and the identical prompt prefixed with the
[KARAKOS_AUTOMATED] sentinel got "I don't know of any secret codeword —
NONE." confirming the skip gate. The slow test at the bottom of this file
automates that same round trip.
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

from conftest import PACKAGE_ROOT, import_script

HOOK_SCRIPT = PACKAGE_ROOT / "system" / "hooks" / "inject-recall.py"
SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
CLAUDE_BIN = shutil.which("claude")

SENTINEL = "[KARAKOS_AUTOMATED]"


def run_hook(prompt: str, env_overrides: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KARAKOS_RECALL", "KARAKOS_AGENT", "KARAKOS_VOICE"))}
    env.update(env_overrides)
    return subprocess.run(
        ["python3", str(HOOK_SCRIPT)],
        input=json.dumps({"prompt": prompt}),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_hook_script_exists_and_executable():
    assert HOOK_SCRIPT.exists()
    mode = HOOK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "hook script must be executable for claude to invoke it"


def test_settings_wires_inject_recall_into_user_prompt_submit():
    config = json.loads(SETTINGS_PATH.read_text())
    entries = config.get("hooks", {}).get("UserPromptSubmit", [])
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert any("inject-recall.py" in cmd for cmd in commands)
    # log-user-prompt.sh must survive alongside it (#94's original hook).
    assert any("log-user-prompt.sh" in cmd for cmd in commands)


def test_missing_recall_source_is_noop(tmp_path):
    missing = tmp_path / "no-such-recall-source"
    assert not missing.exists()
    result = run_hook("hello", {"KARAKOS_RECALL_SOURCE": str(missing)})
    assert result.returncode == 0
    assert result.stdout.strip() == "", f"expected no output, got: {result.stdout!r}"


def test_static_file_recall_source_is_injected_verbatim(tmp_path):
    source = tmp_path / "recall-source"
    source.write_text("Fact: the deploy freeze lifted 7/26.")

    result = run_hook("what changed recently?", {"KARAKOS_RECALL_SOURCE": str(source)})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[ACTIVE RECALL]" in context
    assert "the deploy freeze lifted 7/26" in context


def test_executable_recall_source_receives_prompt_on_stdin(tmp_path):
    source = tmp_path / "recall-source.sh"
    source.write_text(
        "#!/usr/bin/env bash\nread -r line\necho \"got: $line\"\n"
    )
    source.chmod(source.stat().st_mode | stat.S_IXUSR)

    result = run_hook("what is the weather", {"KARAKOS_RECALL_SOURCE": str(source)})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "got: what is the weather" in context


def test_automated_traffic_sentinel_skips_recall_even_with_source_configured(tmp_path):
    source = tmp_path / "recall-source"
    source.write_text("This must never appear for automated traffic.")

    prompt = f"{SENTINEL}\n[2026-08-06T00:00:00Z] heartbeat: check system health"
    result = run_hook(prompt, {"KARAKOS_RECALL_SOURCE": str(source)})
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"recall was injected for automated traffic: {result.stdout!r}"
    )


def test_broken_executable_recall_source_is_noop_not_a_crash(tmp_path):
    source = tmp_path / "recall-source.sh"
    source.write_text("#!/usr/bin/env bash\nexit 1\n")
    source.chmod(source.stat().st_mode | stat.S_IXUSR)

    result = run_hook("hi", {"KARAKOS_RECALL_SOURCE": str(source)})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hanging_executable_recall_source_times_out_as_noop(tmp_path):
    source = tmp_path / "recall-source.sh"
    source.write_text("#!/usr/bin/env bash\nsleep 30\n")
    source.chmod(source.stat().st_mode | stat.S_IXUSR)

    result = run_hook(
        "hi",
        {"KARAKOS_RECALL_SOURCE": str(source), "KARAKOS_RECALL_TIMEOUT_S": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_empty_prompt_is_noop(tmp_path):
    source = tmp_path / "recall-source"
    source.write_text("should never be reached")
    result = run_hook("", {"KARAKOS_RECALL_SOURCE": str(source)})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_sentinel_constant_matches_agent_server(monkeypatch, tmp_workspace):
    """The hook's skip gate and agent-server's stamping logic each hold
    their own copy of the sentinel string (the hook can't import
    agent-server.py — it runs as a standalone process invoked by the real
    claude CLI, not by this test suite's Python interpreter). If either
    copy drifts, automated traffic silently starts paying for recall again
    with no test catching it. This test imports both and asserts equality
    directly, rather than trusting the docstrings to stay honest."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    agent_server = import_script("agent-server")
    inject_recall = import_script("inject-recall", file_path=HOOK_SCRIPT)

    assert agent_server.AUTOMATED_TRAFFIC_SENTINEL == inject_recall.AUTOMATED_TRAFFIC_SENTINEL


# --- graph as the default source (4.2b) ------------------------------------

HEADER = "[ACTIVE RECALL]"


@pytest.fixture
def graph_ws(tmp_path):
    """A workspace dir with an initialised graph holding two distinctive facts."""
    from lib.graph.store import open_graph
    s = open_graph(tmp_path / "data", create=True)
    s.add_observation("the deploy host is called zorblax", importance=8,
                      entity="Deploy", embed=False)
    s.add_observation("the standby pager rotates on tuesdays", importance=6, embed=False)
    return tmp_path


def graph_env(ws, **extra):
    return {"WORKSPACE_ROOT": str(ws), **extra}


def context_of(result):
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "expected a recall block"
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def poison_dir(tmp_path, body):
    d = tmp_path / "poison"
    d.mkdir(exist_ok=True)
    (d / "fastembed.py").write_text(body)
    return d


def test_graph_source_used_when_no_override(graph_ws):
    result = run_hook("where is zorblax", graph_env(graph_ws))
    ctx = context_of(result)
    assert ctx.startswith(HEADER)
    assert "- [fact] Deploy: the deploy host is called zorblax" in ctx


def test_exactly_one_recall_header_in_output(graph_ws):
    result = run_hook("zorblax pager tuesdays", graph_env(graph_ws))
    assert result.stdout.count(HEADER) == 1
    assert len(result.stdout.strip().splitlines()) == 1  # one JSON object


def test_override_file_replaces_graph(graph_ws):
    (graph_ws / "config").mkdir()
    (graph_ws / "config" / "recall-source").write_text("operator block only")
    ctx = context_of(run_hook("where is zorblax", graph_env(graph_ws)))
    assert "operator block only" in ctx
    assert "zorblax" not in ctx


def test_override_env_replaces_graph(graph_ws, tmp_path):
    src = tmp_path / "src"
    src.write_text("env override block")
    ctx = context_of(run_hook("where is zorblax",
                              graph_env(graph_ws, KARAKOS_RECALL_SOURCE=str(src))))
    assert "env override block" in ctx and "zorblax" not in ctx


def test_missing_override_path_is_noop_not_graph(graph_ws, tmp_path):
    result = run_hook("zorblax", graph_env(graph_ws,
                      KARAKOS_RECALL_SOURCE=str(tmp_path / "nope")))
    assert result.stdout.strip() == ""


def test_uninitialised_graph_is_silent_noop(tmp_path):
    result = run_hook("anything", graph_env(tmp_path))
    assert result.returncode == 0 and result.stdout.strip() == ""
    assert result.stderr.strip() == ""
    assert not (tmp_path / "data").exists()  # the hook never creates the graph


def test_broken_graph_file_is_noop(tmp_path):
    p = tmp_path / "data" / "memory" / "graph.db"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\x00not a database" * 100)
    result = run_hook("anything", graph_env(tmp_path))
    assert result.returncode == 0 and result.stdout.strip() == ""


def test_automated_sentinel_skips_graph(graph_ws):
    result = run_hook(f"{SENTINEL}\nzorblax heartbeat", graph_env(graph_ws))
    assert result.returncode == 0 and result.stdout.strip() == ""


def test_fast_mode_never_imports_fastembed(graph_ws, tmp_path):
    marker = tmp_path / "imported"
    d = poison_dir(tmp_path, f"open({str(marker)!r}, 'w').write('x')\nraise ImportError('no')\n")
    result = run_hook("where is zorblax", graph_env(graph_ws, PYTHONPATH=str(d)))
    assert "zorblax" in context_of(result)
    assert not marker.exists(), "fast mode imported fastembed"


def test_full_mode_falls_back_when_model_fails(graph_ws, tmp_path, monkeypatch):
    from lib.graph import embed
    from lib.graph.store import open_graph
    from tests.graph.helpers import FakeTextEmbedding, install_fastembed
    install_fastembed(monkeypatch, FakeTextEmbedding)
    embed._reset()
    open_graph(graph_ws / "data").add_observation("zorblax has embeddings", importance=7)
    embed._reset()
    marker = tmp_path / "imported"
    d = poison_dir(tmp_path, f"open({str(marker)!r}, 'w').write('x')\n"
                   "class TextEmbedding:\n    def __init__(self, *a, **k):\n"
                   "        raise RuntimeError('weights missing')\n")
    result = run_hook("where is zorblax", graph_env(
        graph_ws, PYTHONPATH=str(d), KARAKOS_RECALL_HOOK_MODE="full",
        FASTEMBED_CACHE_PATH=str(tmp_path / "cache")))
    assert "zorblax" in context_of(result)
    assert marker.exists(), "full mode never tried the model"


def test_output_obeys_char_cap(tmp_path):
    from lib.graph.store import open_graph
    s = open_graph(tmp_path / "data", create=True)
    for i in range(30):
        s.add_observation(f"zorblax note {i} " + "padding " * 20, importance=5, embed=False)
    ctx = context_of(run_hook("zorblax", graph_env(tmp_path, KARAKOS_RECALL_MAX_CHARS="300",
                                                  KARAKOS_RECALL_LIMIT="30")))
    body = ctx[len(HEADER):].strip()
    assert 0 < len(body) <= 300


def test_limit_caps_result_count(tmp_path):
    from lib.graph.store import open_graph
    s = open_graph(tmp_path / "data", create=True)
    for i in range(10):
        s.add_observation(f"zorblax item {i}", importance=5, embed=False)
    ctx = context_of(run_hook("zorblax", graph_env(tmp_path, KARAKOS_RECALL_LIMIT="2")))
    assert ctx.count("- [fact]") == 2


def test_human_prompt_gets_block_once_automated_gets_none(graph_ws):
    human = run_hook("where is zorblax", graph_env(graph_ws))
    auto = run_hook(f"{SENTINEL}\nwhere is zorblax", graph_env(graph_ws))
    assert human.stdout.count(HEADER) == 1
    assert auto.stdout.count(HEADER) == 0


@pytest.mark.slow
@pytest.mark.skipif(CLAUDE_BIN is None, reason="claude CLI not installed on this box")
def test_real_claude_dispatch_injects_and_skips_recall(tmp_workspace):
    """The issue's actual acceptance test, automated: a live `claude`
    process, given the package's shipped settings.json and a static
    recall-source, answers a question using injected recall it was never
    told directly — and the same question wrapped in the automated-traffic
    sentinel gets no recall at all.
    """
    hooks_dir = tmp_workspace / "system" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in ("inject-recall.py", "log-user-prompt.sh"):
        dest = hooks_dir / name
        shutil.copy(PACKAGE_ROOT / "system" / "hooks" / name, dest)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR)

    recall_source = tmp_workspace / "config" / "recall-source"
    recall_source.write_text("The secret codeword for today is PINEAPPLE-42.")

    env = dict(os.environ, WORKSPACE_ROOT=str(tmp_workspace))

    result = subprocess.run(
        [CLAUDE_BIN, "-p",
         "What is today's secret codeword? Answer in one short sentence.",
         "--settings", str(SETTINGS_PATH),
         "--dangerously-skip-permissions"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PINEAPPLE-42" in result.stdout, (
        f"recall was never injected into the live session: {result.stdout!r}"
    )

    result_skipped = subprocess.run(
        [CLAUDE_BIN, "-p",
         f"{SENTINEL}\n[2026-08-06T00:00:00Z] heartbeat: What is today's secret "
         "codeword, if you know one? Say NONE if you don't.",
         "--settings", str(SETTINGS_PATH),
         "--dangerously-skip-permissions"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result_skipped.returncode == 0
    assert "PINEAPPLE-42" not in result_skipped.stdout, (
        f"automated traffic still received recall: {result_skipped.stdout!r}"
    )
