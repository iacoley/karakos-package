"""
Tests for issue #92 — an agent can schedule arbitrary future work, and the
schedule survives a restart.

The acceptance test in the issue is two sentences and the second one is the
point: "Remind me in 10 minutes to check the logs" must arrive at ~10 minutes,
AND must still arrive if the container is restarted at minute 5.

So the reboot-survival tests here do not mock anything. They:
  1. write a spool entry through the real `bin/oneshot.py schedule` CLI, in a
     subprocess that then EXITS (nothing is left holding the deadline),
  2. let the deadline pass with no process running at all — that is the
     downtime,
  3. start the real `bin/scheduler.py` entry point fresh, and
  4. assert the scheduled command actually ran, by looking for a file it
     creates on disk.

A mock that cannot fail would prove nothing here, and neither would calling
oneshot.replay() directly — the failure mode this guards is the wiring between
scheduler.main() and the spool, so the test drives scheduler.main().
"""

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT, import_script

ONESHOT_BIN = PACKAGE_ROOT / "bin" / "oneshot.py"
SCHEDULER_BIN = PACKAGE_ROOT / "bin" / "scheduler.py"
TOOLS_SERVER = PACKAGE_ROOT / "mcp" / "tools-server.py"


@pytest.fixture
def oneshot(tmp_path, monkeypatch):
    """The module under test, pointed at a temp spool."""
    monkeypatch.setenv("ONESHOT_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("oneshot")


def _spool(tmp_path):
    return tmp_path / "spool"


# =============================================================================
# The absolute-fire-time invariant
# =============================================================================

def test_spool_stores_absolute_fire_time_not_the_relative_spec(oneshot, tmp_path):
    """The whole reboot story rests on this. A spool entry that stored '+10min'
    and got re-armed at boot would silently slip its deadline by the length of
    the outage."""
    now = 1_700_000_000.0
    entry = oneshot.schedule(label="check-logs", when="10m", command="true", now=now)

    on_disk = json.loads(Path(entry["_path"]).read_text())
    assert on_disk["fire_at"] == int(now + 600)
    assert isinstance(on_disk["fire_at"], int)
    # The typed spec is kept as a note only; it must never be what firing reads.
    assert on_disk["when"] == "10m"


@pytest.mark.parametrize("spec,seconds", [
    ("10m", 600),
    ("+10min", 600),
    ("90s", 90),
    ("2h", 7200),
    ("1h30m", 5400),
    ("3d", 259200),
    ("15 minutes", 900),
])
def test_relative_specs_resolve_to_absolute_times(oneshot, spec, seconds):
    now = 1_700_000_000.0
    assert oneshot.resolve_fire_at(spec, now=now) == int(now + seconds)


def test_absolute_spec_resolves_to_that_wall_clock_time(oneshot):
    fire_at = oneshot.resolve_fire_at("2099-01-02 03:04:05")
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fire_at)) == "2099-01-02 03:04:05"


def test_unparseable_spec_is_refused_not_silently_scheduled(oneshot):
    with pytest.raises(oneshot.OneshotError):
        oneshot.schedule(label="bad", when="sometime soon", command="true")
    assert oneshot.load_entries() == []


def test_past_deadline_is_refused_at_schedule_time(oneshot):
    with pytest.raises(oneshot.OneshotError):
        oneshot.schedule(label="past", when="2001-01-01 00:00:00", command="true")


def test_duplicate_label_does_not_clobber_a_pending_oneshot(oneshot):
    oneshot.schedule(label="dupe", when="1h", command="echo first")
    with pytest.raises(oneshot.OneshotError):
        oneshot.schedule(label="dupe", when="2h", command="echo second")
    assert oneshot.load_entries()[0]["command"] == "echo first"


# =============================================================================
# Firing
# =============================================================================

def test_nothing_fires_before_its_deadline(oneshot):
    now = 1_700_000_000.0
    oneshot.schedule(label="later", when="10m", command="true", now=now)
    fired = []
    results = oneshot.run_due(now=now + 599, runner=lambda cmd: fired.append(cmd) or 0)
    assert results == []
    assert fired == []
    assert len(oneshot.load_entries()) == 1


def test_entry_fires_once_its_deadline_passes_and_leaves_the_spool(oneshot):
    now = 1_700_000_000.0
    oneshot.schedule(label="later", when="10m", command="echo hi", now=now)
    fired = []
    results = oneshot.run_due(now=now + 601, runner=lambda cmd: fired.append(cmd) or 0)
    assert fired == ["echo hi"]
    assert results[0]["action"] == "fired"
    assert oneshot.load_entries() == [], "fired entry must not stay in the spool"


def test_a_failing_command_still_clears_the_spool(oneshot):
    """Fired is fired. Re-running a side-effectful command on the next start
    because it exited nonzero would be worse than the failure."""
    now = 1_700_000_000.0
    oneshot.schedule(label="boom", when="1m", command="exit 3", now=now)
    results = oneshot.run_due(now=now + 61)
    assert results[0]["returncode"] == 3
    assert oneshot.load_entries() == []


def test_missed_deadline_fires_immediately(oneshot):
    """The container was down through the deadline. Twenty minutes late is
    still the thing the user asked for."""
    now = 1_700_000_000.0
    oneshot.schedule(label="missed", when="10m", command="echo hi", now=now)
    fired = []
    results = oneshot.run_due(now=now + 600 + 1200,
                              runner=lambda cmd: fired.append(cmd) or 0)
    assert fired == ["echo hi"]
    assert results[0]["action"] == "fired"
    assert results[0]["late_seconds"] == 1200


def test_very_stale_entry_is_dropped_rather_than_fired(oneshot):
    """A reminder three days late is noise, and a long outage that ends with a
    burst of them is worse than silence. The cutoff is deliberate."""
    now = 1_700_000_000.0
    oneshot.schedule(label="ancient", when="10m", command="echo hi", now=now)
    fired = []
    results = oneshot.run_due(now=now + 600 + (3 * 86400),
                              runner=lambda cmd: fired.append(cmd) or 0)
    assert fired == [], "stale entry must not run its command"
    assert results[0]["action"] == "dropped_stale"
    assert oneshot.load_entries() == []


def test_stale_cutoff_is_configurable(oneshot, monkeypatch):
    monkeypatch.setenv("ONESHOT_STALE_AFTER_SECONDS", str(7 * 86400))
    now = 1_700_000_000.0
    oneshot.schedule(label="ancient", when="10m", command="echo hi", now=now)
    fired = []
    oneshot.run_due(now=now + 600 + (3 * 86400),
                    runner=lambda cmd: fired.append(cmd) or 0)
    assert fired == ["echo hi"]


def test_malformed_entry_is_quarantined_not_fatal(oneshot, tmp_path):
    now = 1_700_000_000.0
    oneshot.schedule(label="good", when="1m", command="echo good", now=now)
    bad = _spool(tmp_path) / "oneshot-broken.oneshot.json"
    bad.write_text("{not json at all")

    fired = []
    oneshot.run_due(now=now + 61, runner=lambda cmd: fired.append(cmd) or 0)

    assert fired == ["echo good"], "one bad entry must not block the good ones"
    assert not bad.exists()
    assert (_spool(tmp_path) / "oneshot-broken.oneshot.json.malformed").exists()


# =============================================================================
# Cancel
# =============================================================================

def test_cancel_removes_the_entry_so_it_never_fires(oneshot):
    now = 1_700_000_000.0
    oneshot.schedule(label="check logs", when="10m", command="echo hi", now=now)
    result = oneshot.cancel(["check logs"])
    assert result["cancelled"] == ["oneshot-check-logs"]

    fired = []
    oneshot.run_due(now=now + 601, runner=lambda cmd: fired.append(cmd) or 0)
    assert fired == [], "a cancelled oneshot must not fire"


def test_cancel_accepts_the_id_form_that_list_prints(oneshot):
    """Pasting back what `list` printed must work — the household original grew
    a bug where 'oneshot-foo' became 'oneshot-oneshot-foo' and cheerfully
    reported nothing to cancel."""
    oneshot.schedule(label="foo", when="10m", command="true")
    assert oneshot.cancel(["oneshot-foo"])["cancelled"] == ["oneshot-foo"]


def test_cancel_of_unknown_label_reports_not_found(oneshot):
    assert oneshot.cancel(["nope"]) == {"cancelled": [], "not_found": ["nope"]}


# =============================================================================
# Message delivery
# =============================================================================

def test_message_becomes_a_poke_invocation(oneshot, tmp_path):
    """A reminder has to land as an unprompted message, not a log line."""
    entry = oneshot.schedule(label="remind", when="10m",
                             message="check the logs", agent="amos")
    assert "bin/poke.sh" in entry["command"]
    assert "check the logs" in entry["command"]
    assert "--agent" in entry["command"] and "amos" in entry["command"]


def test_message_and_command_are_mutually_exclusive(oneshot):
    with pytest.raises(oneshot.OneshotError):
        oneshot.schedule(label="both", when="10m", command="true", message="hi")


# =============================================================================
# Wiring: the scheduler entry point must actually drive this
# =============================================================================

def _main_function(path: Path) -> ast.FunctionDef:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError(f"no main() in {path}")


def _called_names(node: ast.AST) -> set[str]:
    """Names of functions called under `node`. AST, so a call that only appears
    in a comment or docstring does not count."""
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_scheduler_main_replays_the_spool_at_startup():
    assert "replay_oneshots" in _called_names(_main_function(SCHEDULER_BIN))


def test_scheduler_main_loop_polls_for_due_oneshots():
    main = _main_function(SCHEDULER_BIN)
    loops = [n for n in ast.walk(main) if isinstance(n, ast.While)]
    assert loops, "scheduler main() has no loop"
    assert any("run_due_oneshots" in _called_names(loop) for loop in loops), \
        "the main loop never polls the oneshot spool"


def test_scheduler_replay_runs_before_the_main_loop():
    """Order matters: a missed deadline must get its explicit fire-or-drop
    decision at startup, not silently on the first tick."""
    main = _main_function(SCHEDULER_BIN)
    replay_line = min(
        n.lineno for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "replay_oneshots"
    )
    loop_line = min(n.lineno for n in ast.walk(main) if isinstance(n, ast.While))
    assert replay_line < loop_line


# =============================================================================
# Liveness during a slow batch
#
# Firing is synchronous and each command may burn the full 60s exec timeout
# (bin/poke.sh calls curl with no --max-time, so an unreachable relay hits that
# ceiling). The scheduler's liveness heartbeat shares the thread, and
# health-monitor declares scheduler.json stale at 300s. Five slow pokes in one
# batch therefore had the scheduler reported wedged while it was doing exactly
# its job — and the batch is LARGEST right after the longest outage, which is
# the scenario the whole feature exists to serve.
# =============================================================================

def _overdue(oneshot, spool, label, now, command="true"):
    """Spool an entry and back-date it so it is already due."""
    oneshot.schedule(label=label, when="1h", command=command,
                     directory=spool, now=now)
    path = oneshot.entry_path(label, spool)
    entry = json.loads(path.read_text())
    entry["fire_at"] = int(now - 10)
    path.write_text(json.dumps(entry))


def test_a_slow_batch_refreshes_liveness_between_entries(oneshot, tmp_path):
    """The heartbeat must be refreshed per entry, not once after the batch."""
    spool = _spool(tmp_path)
    now = 1_700_000_000.0
    for i in range(3):
        _overdue(oneshot, spool, f"slow-{i}", now, command=f"echo {i}")

    events = []
    oneshot.run_due(
        now=now, directory=spool,
        runner=lambda cmd: (events.append("ran"), 0)[1],
        progress=lambda: events.append("beat"),
    )

    assert events.count("ran") == 3, f"expected 3 commands to fire, got {events}"
    # Interleaved, not all the beats bunched at the end.
    assert events == ["ran", "beat"] * 3, (
        "liveness was not refreshed between entries; a slow batch will let the "
        f"heartbeat go stale mid-run. Sequence was {events}"
    )


def test_scheduler_declares_liveness_before_replaying_the_spool():
    """Replay fires every missed deadline synchronously and can take minutes.
    If the first heartbeat is written only after it, health-monitor reads the
    health file as missing and calls the scheduler dead while it recovers."""
    main = _main_function(SCHEDULER_BIN)

    def first_call(name):
        lines = [n.lineno for n in ast.walk(main)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == name]
        assert lines, f"main() never calls {name}()"
        return min(lines)

    assert first_call("write_health_timestamp") < first_call("replay_oneshots"), \
        "scheduler replays the spool before writing its first heartbeat"


def test_replay_forwards_the_liveness_callback(oneshot, tmp_path):
    """The startup path is the one that matters most here, so the callback must
    actually reach it — replay() must not drop `progress` on the floor."""
    spool = _spool(tmp_path)
    now = 1_700_000_000.0
    _overdue(oneshot, spool, "missed", now, command="echo hi")

    beats = []
    result = oneshot.replay(now=now, directory=spool, runner=lambda cmd: 0,
                            progress=lambda: beats.append(1))

    assert len(result["fired"]) == 1
    assert beats, "replay() did not forward progress to run_due()"


# =============================================================================
# Wiring: the agent-callable primitive
# =============================================================================

def _core_tools() -> list[dict]:
    tree = ast.parse(TOOLS_SERVER.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CORE_TOOLS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("CORE_TOOLS not found")


def test_schedule_is_a_registered_core_tool():
    tools = {t["name"]: t for t in _core_tools()}
    assert "schedule" in tools, "agents cannot call what is not registered"
    actions = tools["schedule"]["inputSchema"]["properties"]["action"]["enum"]
    assert {"create", "list", "cancel"} <= set(actions)


def _tools_server(tmp_workspace, tool, args):
    env = dict(os.environ, WORKSPACE_ROOT=str(tmp_workspace))
    result = subprocess.run(
        [sys.executable, str(TOOLS_SERVER), "--test-tool", tool, json.dumps(args)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_agent_can_create_list_and_cancel_through_the_mcp_tool(tmp_workspace):
    """Drives mcp/tools-server.py's real dispatch entry point, not the helper."""
    (tmp_workspace / "bin").mkdir(exist_ok=True)
    for name in ("oneshot.py", "poke.sh"):
        (tmp_workspace / "bin" / name).write_bytes((PACKAGE_ROOT / "bin" / name).read_bytes())

    created = _tools_server(tmp_workspace, "schedule", {
        "action": "create", "label": "check-logs", "when": "10m",
        "message": "check the logs...",
    })
    assert "error" not in created, created
    assert created["id"] == "oneshot-check-logs"

    listed = _tools_server(tmp_workspace, "schedule", {"action": "list"})
    assert [e["id"] for e in listed["pending"]] == ["oneshot-check-logs"]

    cancelled = _tools_server(tmp_workspace, "schedule",
                              {"action": "cancel", "label": "check-logs"})
    assert cancelled["cancelled"] == ["oneshot-check-logs"]
    assert _tools_server(tmp_workspace, "schedule", {"action": "list"})["pending"] == []


def test_mcp_tool_rejects_a_create_with_no_when(tmp_workspace):
    (tmp_workspace / "bin").mkdir(exist_ok=True)
    (tmp_workspace / "bin" / "oneshot.py").write_bytes(ONESHOT_BIN.read_bytes())
    result = _tools_server(tmp_workspace, "schedule",
                           {"action": "create", "label": "x", "message": "hi"})
    assert "error" in result


def test_a_reminder_containing_an_ellipsis_is_not_rejected_as_path_traversal():
    """"check the logs..." is prose, not a parent directory. The shared arg
    validator used to reject any string containing '..'."""
    server = import_script("tools-server")
    schema = {t["name"]: t for t in server.CORE_TOOLS}["schedule"]["inputSchema"]
    assert server.validate_args(
        {"action": "create", "label": "x", "when": "10m",
         "message": "check the logs..."}, schema) is None


# =============================================================================
# Reboot survival — the second sentence of the acceptance test
# =============================================================================

def _run_scheduler(workspace: Path, spool: Path) -> subprocess.Popen:
    env = dict(
        os.environ,
        WORKSPACE_ROOT=str(workspace),
        ONESHOT_SPOOL_DIR=str(spool),
        SCHEDULER_TICK_SECONDS="1",
    )
    return subprocess.Popen(
        [sys.executable, str(SCHEDULER_BIN)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return False


def test_a_oneshot_survives_the_process_that_scheduled_it_dying(tmp_workspace):
    """The reboot half of the acceptance test, with no mocks.

    Two entries are written by the real `oneshot.py schedule` CLI in a
    subprocess that then exits. Nothing runs while their deadlines approach —
    that is the downtime. Then the real bin/scheduler.py starts fresh and must:

      - fire the entry whose deadline passed while everything was down, and
      - still honour the entry whose deadline is yet to come.

    If the spool stored a relative delay instead of an absolute time, the
    already-due entry would be re-anchored to the scheduler's start and the
    first assertion would time out.
    """
    spool = tmp_workspace / "spool"
    missed_sentinel = tmp_workspace / "missed-fired"
    future_sentinel = tmp_workspace / "future-fired"

    env = dict(os.environ, WORKSPACE_ROOT=str(tmp_workspace),
               ONESHOT_SPOOL_DIR=str(spool))
    for label, when, sentinel in [
        ("missed-while-down", "3s", missed_sentinel),
        ("still-pending", "8s", future_sentinel),
    ]:
        done = subprocess.run(
            [sys.executable, str(ONESHOT_BIN), "schedule",
             "--label", label, "--when", when,
             "--command", f"touch {sentinel}"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert done.returncode == 0, done.stderr

    # The scheduling process has exited. Nothing at all is running while the
    # first deadline passes — this is the container being down.
    time.sleep(4.5)
    assert not missed_sentinel.exists(), "nothing should have fired with no scheduler running"
    assert len(list(spool.glob("*.oneshot.json"))) == 2

    proc = _run_scheduler(tmp_workspace, spool)
    fired_missed = fired_future = False
    try:
        fired_missed = _wait_for(missed_sentinel, 20)
        fired_future = _wait_for(future_sentinel, 20)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        output = proc.stdout.read()

    assert fired_missed, f"missed-deadline oneshot never fired after restart:\n{output}"
    assert fired_future, f"still-pending oneshot never fired after restart:\n{output}"
    assert list(spool.glob("*.oneshot.json")) == [], "fired entries must be cleared"

    # The startup pass, not just the steady-state tick, is what accounted for
    # the inherited spool: one deadline missed while down, one still ahead.
    assert "Oneshot spool restored: 1 pending, 1 fired late, 0 dropped stale" in output, \
        f"startup replay did not report the inherited spool:\n{output}"


def test_a_stale_oneshot_is_not_resurrected_by_a_restart(tmp_workspace):
    """The other half of the deliberate missed-deadline choice, end to end:
    a deadline missed by more than the cutoff is dropped by the real scheduler
    startup path, not fired."""
    spool = tmp_workspace / "spool"
    sentinel = tmp_workspace / "stale-fired"

    env = dict(os.environ, WORKSPACE_ROOT=str(tmp_workspace),
               ONESHOT_SPOOL_DIR=str(spool))
    done = subprocess.run(
        [sys.executable, str(ONESHOT_BIN), "schedule", "--label", "stale",
         "--when", "1h", "--command", f"touch {sentinel}"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert done.returncode == 0, done.stderr

    # Simulate a very long outage by making the entry's absolute deadline old.
    # The entry itself was written by the real schedule path; only the clock
    # is being moved, which is the one thing a test cannot wait out.
    entry_path = next(spool.glob("*.oneshot.json"))
    entry = json.loads(entry_path.read_text())
    entry["fire_at"] = int(time.time()) - (5 * 86400)
    entry_path.write_text(json.dumps(entry))

    proc = _run_scheduler(tmp_workspace, spool)
    try:
        fired = _wait_for(sentinel, 8)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        output = proc.stdout.read()

    assert not fired, f"a five-day-stale reminder must not fire:\n{output}"
    assert list(spool.glob("*.oneshot.json")) == [], "stale entry must be cleared, not left to retry"
    assert "Oneshot spool restored: 0 pending, 0 fired late, 1 dropped stale" in output, \
        f"startup replay did not report the drop:\n{output}"
