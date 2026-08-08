"""
Tests for wedged-agent detection (#104).

Acceptance test from the issue: SIGSTOP the `claude` subprocess, then send a
message. Pass = within two minutes an alert names that agent as wedged.

A SIGSTOPped subprocess is exactly the failure the existing checks are blind
to: the process is alive, the port answers, `/health` returns 200, and the
user's message goes nowhere. So the tests below are written around what that
looks like on disk — a beacon whose state stays PROCESSING while its
timestamp stops moving — rather than around whether a process exists.

Three properties are load-bearing and each is pinned:

1. **Idle silence is not a wedge.** An IDLE agent writes nothing for hours and
   that is correct. A check that pages for it would page every night, and a
   check that cries wolf is worse than none.
2. **The alert never goes through poke.sh.** poke.sh queues a message *for an
   agent*; if that agent is the wedged one, the alert lands in the queue it
   cannot read. The check would then fail silently in precisely the case it
   exists for.
3. **It never restarts anything.** Detection and escalation only.
"""

import ast
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
WEDGE_CHECK = PACKAGE_ROOT / "bin" / "wedge-check.py"
SCHEDULER = PACKAGE_ROOT / "bin" / "scheduler.py"
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"


@pytest.fixture
def wedge(tmp_path):
    """Import bin/wedge-check.py against a temp workspace."""
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("wedge_under_test", WEDGE_CHECK)
        module = importlib.util.module_from_spec(spec)
        sys.modules["wedge_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


def write_beacon(wedge, agent, state, age_seconds, message_id="m-1"):
    """Write a beacon as the agent server writes it, aged by `age_seconds`."""
    wedge.BEACON_DIR.mkdir(parents=True, exist_ok=True)
    last = datetime.now() - timedelta(seconds=age_seconds)
    (wedge.BEACON_DIR / f"{agent}.json").write_text(json.dumps({
        "agent": agent,
        "state": state,
        "last_activity": last.isoformat(),
        "message_id": message_id,
        "pid": 1234,
    }))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_a_stopped_subprocess_shows_up_as_wedged(wedge):
    # The acceptance test, as it appears on disk: the turn was claimed, the
    # events stopped. Nothing about the process is visibly wrong.
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=300)
    wedged = wedge.find_wedged(threshold_sec=120)

    assert [b["agent"] for b in wedged] == ["amos"]
    assert wedged[0]["silent_for"] > 120


def test_an_idle_agent_silent_for_hours_is_not_wedged(wedge):
    # The single most important negative. An idle agent is silent by
    # definition; paging for it would page every night.
    write_beacon(wedge, "amos", "IDLE", age_seconds=86400)
    assert wedge.find_wedged(threshold_sec=120) == []


def test_an_agent_still_emitting_events_is_not_wedged(wedge):
    # A long turn that is still moving — the beacon advanced 10s ago.
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=10)
    assert wedge.find_wedged(threshold_sec=120) == []


def test_a_turn_quiet_but_inside_the_threshold_is_left_alone(wedge):
    # A long Bash call emits nothing until it returns. Under the threshold
    # that is a working agent, not a wedge.
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=119)
    assert wedge.find_wedged(threshold_sec=120) == []


def test_error_recovery_counts_as_a_claimed_turn(wedge):
    write_beacon(wedge, "amos", "ERROR_RECOVERY", age_seconds=300)
    assert len(wedge.find_wedged(threshold_sec=120)) == 1


def test_several_agents_are_all_reported_worst_first(wedge):
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=200)
    write_beacon(wedge, "herald", "PROCESSING", age_seconds=900)
    write_beacon(wedge, "argus", "IDLE", age_seconds=99999)

    wedged = wedge.find_wedged(threshold_sec=120)
    assert [b["agent"] for b in wedged] == ["herald", "amos"]


def test_no_beacons_at_all_is_quiet(wedge):
    # A fresh install, or nothing has run yet. Not a wedge.
    assert wedge.find_wedged(threshold_sec=120) == []


@pytest.mark.parametrize("junk", ["{not json", "[]", '{"state": "PROCESSING"}', ""])
def test_an_unreadable_beacon_is_not_treated_as_a_wedge(wedge, junk):
    # Beacons are written atomically via rename, so a malformed one means
    # something else is wrong. Inventing a wedge from it pages for the wrong
    # reason, and the operator learns to ignore the alert.
    wedge.BEACON_DIR.mkdir(parents=True, exist_ok=True)
    (wedge.BEACON_DIR / "amos.json").write_text(junk)
    assert wedge.find_wedged(threshold_sec=120) == []


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def test_the_alert_names_the_agent_and_says_no_restart_was_attempted(wedge):
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=300)
    text = wedge.format_alert(wedge.find_wedged(threshold_sec=120))

    assert "amos" in text
    assert "No restart" in text


def test_the_same_wedge_alerts_once_not_every_minute(wedge, monkeypatch):
    sent = []
    monkeypatch.setattr(wedge, "send_alert", lambda msg: sent.append(msg) or True)
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=300)

    # Running every minute, as the scheduler does, against a beacon that by
    # definition is not moving.
    for _ in range(5):
        assert wedge.main([]) == 1

    assert len(sent) == 1


def test_a_second_wedge_after_a_recovery_alerts_again(wedge, monkeypatch):
    # Keyed on the frozen timestamp, not the agent name: keying on the agent
    # would silence every future wedge of that agent forever.
    sent = []
    monkeypatch.setattr(wedge, "send_alert", lambda msg: sent.append(msg) or True)

    write_beacon(wedge, "amos", "PROCESSING", age_seconds=300)
    assert wedge.main([]) == 1
    # Recovered, ran again, wedged again — a different last_activity.
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=299)
    assert wedge.main([]) == 1

    assert len(sent) == 2


def test_a_failed_alert_is_not_remembered_as_delivered(wedge, monkeypatch):
    # Otherwise one Discord outage converts a wedge into permanent silence.
    attempts = []
    monkeypatch.setattr(wedge, "send_alert", lambda msg: attempts.append(msg) or False)
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=300)

    wedge.main([])
    wedge.main([])
    assert len(attempts) == 2


def test_no_alert_flag_stays_silent(wedge, monkeypatch):
    sent = []
    monkeypatch.setattr(wedge, "send_alert", lambda msg: sent.append(msg) or True)
    write_beacon(wedge, "amos", "PROCESSING", age_seconds=300)

    assert wedge.main(["--no-alert"]) == 1
    assert sent == []


def test_a_healthy_install_exits_zero(wedge):
    write_beacon(wedge, "amos", "IDLE", age_seconds=5000)
    assert wedge.main([]) == 0


# ---------------------------------------------------------------------------
# The three design properties, checked structurally
# ---------------------------------------------------------------------------

def _non_docstring_str_literals(tree):
    """Every string constant except docstrings."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    ]


def _names_called(tree, func_name):
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
    )
    return {
        getattr(c.func, "id", None) or getattr(c.func, "attr", None)
        for c in ast.walk(node) if isinstance(c, ast.Call)
    }


def test_the_alert_path_does_not_go_through_the_agent_queue():
    """poke.sh queues a message FOR AN AGENT.

    If that agent is the wedged one, the alert lands in the queue it cannot
    read and is never seen — the check fails silently in exactly the case it
    exists for. health-monitor.py's existing `poke_signals` has this shape;
    this script must not.
    """
    tree = ast.parse(WEDGE_CHECK.read_text())
    # Docstrings are excluded rather than substring-filtered: this module
    # discusses poke.sh at length precisely to explain why it is not used,
    # and a naive grep would read that explanation as the thing it warns
    # against.
    literals = _non_docstring_str_literals(tree)
    assert not any("poke.sh" in lit for lit in literals)
    assert any("discord-notify.sh" in lit for lit in literals)


def test_the_watcher_never_restarts_anything():
    """Detection and escalation only — an operator decides on a restart.

    An automatic restart discards whatever the agent was mid-way through.
    """
    literals = _non_docstring_str_literals(ast.parse(WEDGE_CHECK.read_text()))
    for forbidden in ("systemctl", "supervisorctl", "/reset", "/reload", "kill"):
        assert not any(forbidden in lit for lit in literals), forbidden


def test_the_check_is_scheduled_often_enough_to_meet_its_acceptance_test():
    """Two minutes is the promise; the health sweep runs at 04:00 daily.

    Without a minute-scale cadence the detection is correct and unreachable,
    which is the same as absent for 1439 minutes of the day.
    """
    tree = ast.parse(SCHEDULER.read_text())
    assert "run_wedge_check" in {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    src = SCHEDULER.read_text()
    assert "schedule.every(1).minutes.do(run_wedge_check)" in src
    assert "wedge-check.py" in _collect_str_literals(tree)


def _collect_str_literals(tree):
    return " ".join(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


# ---------------------------------------------------------------------------
# The beacon the watcher depends on
# ---------------------------------------------------------------------------

def test_the_agent_server_writes_a_beacon_from_its_stream_loop():
    """Without this the beacon never moves and every busy agent reads wedged.

    The stream loop is the right place because it is the loop that goes quiet:
    a SIGSTOPped claude blocks its readline, so the timestamp stops advancing
    while the state stays PROCESSING — which is the exact pair `find_wedged`
    keys on.
    """
    tree = ast.parse(AGENT_SERVER.read_text())
    assert "write_agent_beacon" in _names_called(tree, "read_agent_response")


def test_the_agent_server_marks_the_agent_idle_when_a_turn_ends():
    """The other half. If the state never returns to IDLE, every completed
    turn leaves a beacon that looks wedged forever — and the check would page
    about every healthy agent that had ever answered anything.
    """
    tree = ast.parse(AGENT_SERVER.read_text())
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "read_agent_response"
    )
    states_written = {
        c.args[1].value
        for c in ast.walk(node)
        if isinstance(c, ast.Call)
        and getattr(c.func, "id", None) == "write_agent_beacon"
        and len(c.args) > 1 and isinstance(c.args[1], ast.Constant)
    }
    assert "IDLE" in states_written
    assert "PROCESSING" in states_written


def test_startup_clears_a_beacon_left_behind_by_a_crash():
    """A crash mid-turn leaves a beacon reading PROCESSING with a timestamp
    that will never advance — indistinguishable from a live wedge. Without an
    overwrite at startup, every restart-after-crash pages forever about an
    agent that is now perfectly fine.
    """
    tree = ast.parse(AGENT_SERVER.read_text())
    assert "write_agent_beacon" in _names_called(tree, "startup")


def test_a_beacon_write_failure_cannot_break_the_turn_it_reports_on(tmp_path):
    """A beacon that could crash a reply would be worse than no beacon."""
    prev = os.environ.get("WORKSPACE_ROOT")
    workspace = tmp_path / "ws"
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("ags_beacon_under_test", AGENT_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_beacon_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev

    # Point the beacon dir at a path that cannot be created.
    module.AGENT_BEACON_DIR = Path("/proc/nonexistent/beacons")
    module.write_agent_beacon("amos", "PROCESSING", force=True)  # must not raise


def test_the_beacon_records_the_state_and_not_only_a_timestamp(tmp_path):
    """Staleness alone cannot tell idle from wedged.

    health-monitor.py already reads timestamps out of data/health/*.json and
    still cannot see this failure, because an idle agent and a wedged one are
    both silent. The state is what separates them.
    """
    prev = os.environ.get("WORKSPACE_ROOT")
    workspace = tmp_path / "ws2"
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("ags_beacon2_under_test", AGENT_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_beacon2_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev

    module.write_agent_beacon("amos", "PROCESSING", message_id="m-9", force=True)
    written = json.loads((module.AGENT_BEACON_DIR / "amos.json").read_text())

    assert written["state"] == "PROCESSING"
    assert written["message_id"] == "m-9"
    assert written["last_activity"]
    assert written["pid"] == os.getpid()
