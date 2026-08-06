"""
Tests for issue #97 — Stop hook catching a promised-but-never-done turn.

An agent that ends its reply with "I'll check that shortly" gets its turn
extended (via {"decision": "block"}) instead of the promise just evaporating
when the turn ends. Capped at 2 extensions per session so a model that keeps
re-deferring in its continuation can't loop forever.

Each test invokes the real hook script via subprocess with the exact JSON
Claude Code sends a Stop hook on stdin (session_id, transcript_path,
stop_hook_active), against a real temp transcript file, and asserts on the
real stdout. Removing or breaking system/hooks/stop-deferred-work.py fails
every test here with a nonzero/FileNotFoundError, not a mock mismatch.

WORKSPACE_ROOT is pointed at a fresh tmp_path per test so the on-disk
extension counter (data/stop-hook-extensions.json) never leaks state
between tests.
"""

import json
import subprocess
import uuid
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent
HOOK = PACKAGE_ROOT / "system" / "hooks" / "stop-deferred-work.py"
SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"


def write_transcript(tmp_path, entries):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def run_hook(payload, workspace_root):
    result = subprocess.run(
        ["python3", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env={"WORKSPACE_ROOT": str(workspace_root), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_hook_script_exists_and_is_executable():
    import stat
    assert HOOK.exists()
    assert HOOK.stat().st_mode & stat.S_IXUSR


def test_settings_file_wires_stop_deferred_work():
    config = json.loads(SETTINGS_PATH.read_text())
    entries = config["hooks"]["Stop"]
    assert entries, "Stop hook list is empty"
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert any("stop-deferred-work.py" in c for c in commands)


def test_deferral_phrase_extends_the_turn(tmp_path):
    transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "fix the bug", "uuid": "u1"},
        {"role": "assistant", "content": "On it."},
    ])
    out = run_hook(
        {"session_id": str(uuid.uuid4()), "transcript_path": str(transcript),
         "stop_hook_active": False},
        tmp_path,
    )
    result = json.loads(out)
    assert result["decision"] == "block"
    assert "On it" in result["reason"]
    assert "1/2" in result["reason"]


def test_plain_completed_reply_does_not_extend(tmp_path):
    transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "fix the bug", "uuid": "u1"},
        {"role": "assistant", "content": "Fixed. Deployed and verified."},
    ])
    out = run_hook(
        {"session_id": str(uuid.uuid4()), "transcript_path": str(transcript),
         "stop_hook_active": False},
        tmp_path,
    )
    assert out == ""


def test_extension_cap_stops_after_two_in_the_same_turn(tmp_path):
    transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "fix the bug", "uuid": "u1"},
        {"role": "assistant", "content": "Let me handle that now."},
    ])
    session_id = str(uuid.uuid4())
    payload = {"session_id": session_id, "transcript_path": str(transcript),
               "stop_hook_active": False}

    first = json.loads(run_hook(payload, tmp_path))
    assert first["decision"] == "block"
    assert "1/2" in first["reason"]

    second = json.loads(run_hook(payload, tmp_path))
    assert second["decision"] == "block"
    assert "2/2" in second["reason"]

    # Third consecutive hit on the SAME user turn: cap reached, no block.
    third = run_hook(payload, tmp_path)
    assert third == "", f"expected the cap to hold, got: {third!r}"


def test_new_user_message_resets_the_cap(tmp_path):
    session_id = str(uuid.uuid4())
    first_transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "fix the bug", "uuid": "u1"},
        {"role": "assistant", "content": "On it."},
    ])
    payload = {"session_id": session_id, "transcript_path": str(first_transcript),
               "stop_hook_active": False}
    run_hook(payload, tmp_path)
    run_hook(payload, tmp_path)
    capped = run_hook(payload, tmp_path)
    assert capped == ""

    # A NEW user message arrives — a different request, not a continuation —
    # so the cap must not still apply to it.
    second_transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "fix the bug", "uuid": "u1"},
        {"role": "assistant", "content": "On it."},
        {"role": "user", "content": "now do the other thing", "uuid": "u2"},
        {"role": "assistant", "content": "I'll get to it shortly."},
    ])
    payload2 = {"session_id": session_id, "transcript_path": str(second_transcript),
                "stop_hook_active": False}
    fresh = json.loads(run_hook(payload2, tmp_path))
    assert fresh["decision"] == "block"
    assert "1/2" in fresh["reason"]


def test_stop_hook_active_true_blocks_further_extension(tmp_path):
    """Independent backstop: if the harness reports it already forced one
    continuation this stop cycle, this hook must not force another —
    even on a fresh session with no counter history at all."""
    transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "fix the bug", "uuid": "u1"},
        {"role": "assistant", "content": "On it."},
    ])
    out = run_hook(
        {"session_id": str(uuid.uuid4()), "transcript_path": str(transcript),
         "stop_hook_active": True},
        tmp_path,
    )
    assert out == ""


def test_quoted_example_deferral_does_not_self_trigger(tmp_path):
    transcript = write_transcript(tmp_path, [
        {"role": "user", "content": "what counts as a deferral?", "uuid": "u1"},
        {"role": "assistant", "content": 'A phrase like "On it." counts as a deferral, but I have fully answered your question and there is nothing further to do.'},
    ])
    out = run_hook(
        {"session_id": str(uuid.uuid4()), "transcript_path": str(transcript),
         "stop_hook_active": False},
        tmp_path,
    )
    assert out == ""


def test_missing_transcript_path_is_a_noop(tmp_path):
    out = run_hook(
        {"session_id": str(uuid.uuid4()), "transcript_path": "", "stop_hook_active": False},
        tmp_path,
    )
    assert out == ""


def test_malformed_json_stdin_does_not_crash(tmp_path):
    result = subprocess.run(
        ["python3", str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
        env={"WORKSPACE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert result.stdout == ""
