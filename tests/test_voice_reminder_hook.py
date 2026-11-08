"""inject-recall.py appends an agent's voice reminder (+ random exemplars) to every
human turn, independent of recall. Opt-in by file; automated traffic skips it."""

import json
import os
import subprocess

from conftest import PACKAGE_ROOT

HOOK = PACKAGE_ROOT / "system" / "hooks" / "inject-recall.py"
SENTINEL = "[KARAKOS_AUTOMATED]"


def run(ws, prompt, agent="helper", **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KARAKOS_RECALL", "KARAKOS_VOICE"))}
    # Empty static recall source: voice must not depend on recall having content.
    src = ws / "recall-source"
    src.write_text("")
    env.update({"WORKSPACE_ROOT": str(ws), "KARAKOS_RECALL_SOURCE": str(src)})
    if agent:
        env["KARAKOS_AGENT"] = agent
    env.update(extra)
    return subprocess.run(["python3", str(HOOK)], input=json.dumps({"prompt": prompt}),
                          env=env, capture_output=True, text=True, timeout=15)


def ctx(result):
    assert result.returncode == 0
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def agent_dir(ws, agent="helper"):
    d = ws / "agents" / agent
    d.mkdir(parents=True)
    return d


def test_reminder_injected_even_with_empty_recall(tmp_path):
    (agent_dir(tmp_path) / "voice_reminder.md").write_text("Stay dry.")
    out = ctx(run(tmp_path, "hello"))
    assert "[VOICE]" in out and "Stay dry." in out
    assert "[ACTIVE RECALL]" not in out


def test_no_files_means_no_output(tmp_path):
    assert run(tmp_path, "hello").stdout.strip() == ""


def test_voice_comes_after_recall(tmp_path):
    (agent_dir(tmp_path) / "voice_reminder.md").write_text("Stay dry.")
    (tmp_path / "recall-source").write_text("x")
    env = {"KARAKOS_RECALL_SOURCE": str(tmp_path / "rs")}
    (tmp_path / "rs").write_text("A fact.")
    out = ctx(run(tmp_path, "hello", **env))
    assert out.index("[ACTIVE RECALL]") < out.index("[VOICE]")


def test_exemplars_sampled_and_capped(tmp_path):
    d = agent_dir(tmp_path)
    (d / "voice_reminder.md").write_text("Stay dry.")
    (d / "voice_exemplars.md").write_text("one\n---\ntwo\n---\nthree\n---\nfour\n")
    out = ctx(run(tmp_path, "hello", KARAKOS_VOICE_EXEMPLARS="2"))
    assert sum(w in out for w in ("one", "two", "three", "four")) == 2
    out0 = ctx(run(tmp_path, "hello", KARAKOS_VOICE_EXEMPLARS="0"))
    assert not any(w in out0 for w in ("one", "two", "three", "four"))


def test_automated_traffic_skips_voice(tmp_path):
    (agent_dir(tmp_path) / "voice_reminder.md").write_text("Stay dry.")
    assert run(tmp_path, f"{SENTINEL}\nheartbeat").stdout.strip() == ""


def test_other_agent_and_bad_agent_id_get_nothing(tmp_path):
    (agent_dir(tmp_path) / "voice_reminder.md").write_text("Stay dry.")
    assert run(tmp_path, "hi", agent="someone-else").stdout.strip() == ""
    assert run(tmp_path, "hi", agent="../helper").stdout.strip() == ""
    assert run(tmp_path, "hi", agent=None).stdout.strip() == ""
