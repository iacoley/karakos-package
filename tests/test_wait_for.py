"""
Tests for issue #93 — bin/wait-for.sh, the sanctioned way to wait on a
condition (or a fixed delay via --sleep) instead of a sandbox-blocked
foreground `sleep`.

Ships first because #96 (rewrite_sleep_poll PreToolUse hook) rewrites
blocked sleep polls to point at this script; if it isn't here yet, the
rewrite just relocates the failure.

Each test invokes the real script as a subprocess, so removing
bin/wait-for.sh (or breaking its argument handling) fails every test here
with a nonzero/FileNotFoundError, not a mock mismatch.
"""

import subprocess
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent
WAIT_FOR = PACKAGE_ROOT / "bin" / "wait-for.sh"

MET = 0
TIMED_OUT = 124


def run_wait(args, timeout_s=15):
    return subprocess.run(
        [str(WAIT_FOR), *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def test_script_exists_and_is_executable():
    assert WAIT_FOR.exists()
    import stat
    assert WAIT_FOR.stat().st_mode & stat.S_IXUSR


def test_condition_already_true_returns_immediately():
    start = time.time()
    result = run_wait(["true", "--interval", "1", "--timeout", "5"])
    elapsed = time.time() - start
    assert result.returncode == MET
    assert elapsed < 2, f"a true condition should not wait a full interval, took {elapsed}s"


def test_condition_becomes_true_mid_wait(tmp_path):
    marker = tmp_path / "ready"
    proc = subprocess.Popen(["bash", "-c", f"sleep 1; touch {marker}"])
    try:
        result = run_wait([f"test -f {marker}", "--interval", "1", "--timeout", "10"])
        assert result.returncode == MET
        assert marker.exists()
    finally:
        proc.wait(timeout=10)


def test_condition_never_true_times_out():
    start = time.time()
    result = run_wait(["false", "--timeout", "2", "--interval", "1"])
    elapsed = time.time() - start
    assert result.returncode == TIMED_OUT
    assert elapsed >= 2
    assert "TIMEOUT" in result.stderr


def test_sleep_waits_approximately_n_seconds():
    start = time.time()
    result = run_wait(["--sleep", "2"])
    elapsed = time.time() - start
    assert result.returncode == MET
    assert 2 <= elapsed < 5, f"expected ~2s, took {elapsed}s"


def test_tail_file_printed_on_success(tmp_path):
    log = tmp_path / "out.log"
    log.write_text("line one\nline two\n")
    result = run_wait(["true", "--tail", str(log)])
    assert result.returncode == MET
    assert "line two" in result.stdout


def test_pgrep_self_match_guard_does_not_deadlock():
    """The condition string sits in wait-for.sh's own argv. A naive `pgrep -f`
    on a marker unique to this test would match wait-for.sh itself forever.
    The shadowed pgrep must drop self/ancestor/fork matches so the negated
    wait still fires."""
    marker = "zzq-wait-for-test-selfmatch-marker"
    result = run_wait([f"! pgrep -f '{marker}'", "--timeout", "5", "--interval", "1"])
    assert result.returncode == MET, result.stderr


def test_pgrep_guard_still_sees_a_real_unrelated_process():
    """The guard must drop only self/ancestor/fork matches, not blind the
    wait to every process carrying the marker."""
    marker = "zzq-wait-for-test-realproc-marker"
    helper = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)", marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("helper process never became visible to pgrep")

        result = run_wait([f"pgrep -f '{marker}'", "--timeout", "5", "--interval", "1"])
        assert result.returncode == MET
    finally:
        helper.kill()
        helper.wait(timeout=10)
