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
    env = dict(os.environ)
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
