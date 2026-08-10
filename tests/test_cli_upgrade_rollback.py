"""
Tests for the Claude CLI rollback guard (#106).

Acceptance test from the issue: point the updater at a known-bad Claude CLI
version, run the update, send a Discord message. Pass = it is answered, and
the channel carries a notice that the upgrade was reverted.

"It is answered" is the part that constrains the design. A bad upstream
release installs cleanly, answers `claude --version`, keeps every supervisord
process up and /health at 200 — and never completes another turn. So the tests
below stand up a fake `claude` on PATH that behaves exactly that way, and
assert on what the real scripts do about it: reinstall the version that
worked, and say so somewhere a human will actually look.

Everything here drives the real scripts as subprocesses. `npm`, `claude`,
`discord-notify.sh` and `poke.sh` are recording stand-ins, so deleting either
script or breaking its flow fails these tests with a missing-file or
wrong-recording error rather than a mock mismatch.

Three properties are load-bearing and each is pinned:

1. **A version string is not a health check.** The probe is a real turn over
   the same stream-json wire agent-server.py uses. A CLI that installs and
   reports a version but cannot answer must be rolled back.
2. **The notice never goes through poke.sh.** poke.sh queues a message FOR AN
   AGENT. The situation being reported is that agents cannot complete a turn,
   so that queue is the one place the notice is guaranteed not to be read.
3. **A working upgrade is left alone.** A guard that reverts on a transient
   API blip is an outage generator, not a safety net.
"""

import ast
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
UPGRADE = PACKAGE_ROOT / "bin" / "upgrade-claude-cli.sh"
WATCHDOG = PACKAGE_ROOT / "bin" / "cli-upgrade-watchdog.sh"
SCHEDULER = PACKAGE_ROOT / "bin" / "scheduler.py"

# Exit codes both scripts share.
VERIFIED = 0
REVERTED = 1
REVERT_FAILED = 2
CANNOT_RUN = 3

GOOD = "1.0.0"
BAD = "9.9.9"


# =============================================================================
# The fake world: an npm that installs, a claude that may or may not work,
# and two recording notifiers.
# =============================================================================

def write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def world(tmp_path):
    """A PATH where `claude` and `npm` are ours, plus recording notifiers.

    `broken` lists the versions whose CLI installs fine, answers --version,
    and then cannot complete a turn — the shape of the failure being guarded.
    """
    binp = tmp_path / "bin"
    binp.mkdir()
    (tmp_path / "state").mkdir()

    installed = tmp_path / "state" / "installed"
    installed.write_text(GOOD)
    broken = tmp_path / "state" / "broken"
    broken.write_text("")
    npm_log = tmp_path / "state" / "npm.log"
    npm_fail = tmp_path / "state" / "npm-fails-for"
    npm_fail.write_text("")
    claude_log = tmp_path / "state" / "claude.log"

    write_exec(binp / "npm", f"""#!/usr/bin/env bash
pkg=""
for arg in "$@"; do case "$arg" in *@*) pkg="$arg";; esac; done
version="${{pkg##*@}}"
echo "install $version" >> "{npm_log}"
if grep -qx "$version" "{npm_fail}"; then
    echo "npm ERR! not found" >&2
    exit 1
fi
echo "$version" > "{installed}"
""")

    write_exec(binp / "claude", f"""#!/usr/bin/env bash
version=$(cat "{installed}")
if [[ "${{1:-}}" == "--version" ]]; then
    echo "$version (Claude Code)"
    exit 0
fi
echo "turn $version" >> "{claude_log}"
payload=$(cat)
echo "$payload" >> "{claude_log}"
if grep -qx "$version" "{broken}"; then
    # Installs, reports a version, never completes a turn.
    echo "Error: agent loop failed to start" >&2
    exit 1
fi
echo '{{"type":"assistant","message":{{"content":[{{"type":"text","text":"KARAKOS_CLI_OK"}}]}}}}'
echo '{{"type":"result","subtype":"success","is_error":false,"result":"KARAKOS_CLI_OK"}}'
""")

    notify_dir = tmp_path / "notify"
    notify_dir.mkdir()
    notify_log = notify_dir / "notify.log"
    poke_log = notify_dir / "poke.log"
    write_exec(notify_dir / "discord-notify.sh",
               f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{notify_log}"\n')
    write_exec(notify_dir / "poke.sh",
               f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{poke_log}"\n')

    class World:
        root = tmp_path
        state_file = tmp_path / "claude-cli.json"

        def env(self):
            return {
                **os.environ,
                "PATH": f"{binp}:{os.environ['PATH']}",
                "KARAKOS_NOTIFY_BIN_DIR": str(notify_dir),
                "CLAUDE_CLI_STATE_FILE": str(self.state_file),
                "WORKSPACE_ROOT": str(tmp_path / "workspace"),
                "CLI_VERIFY_MODEL": "sonnet",
                "CLI_VERIFY_ATTEMPTS": "1",
                "CLI_VERIFY_RETRY_DELAY": "0",
            }

        def run(self, script, *args, **envextra):
            return subprocess.run(
                ["bash", str(script), *args],
                capture_output=True, text=True, timeout=120,
                env={**self.env(), **envextra},
            )

        @property
        def installed(self):
            return installed.read_text().strip()

        @installed.setter
        def installed(self, value):
            installed.write_text(value)

        def break_version(self, version):
            broken.write_text(broken.read_text() + "\n" + version)

        def unbreak_all(self):
            broken.write_text("")

        def fail_npm_for(self, version):
            npm_fail.write_text(version)

        @property
        def installs(self):
            if not npm_log.exists():
                return []
            return [l.split()[1] for l in npm_log.read_text().splitlines() if l.strip()]

        def clear_installs(self):
            npm_log.write_text("")

        @property
        def notices(self):
            if not notify_log.exists():
                return []
            return [l for l in notify_log.read_text().splitlines() if l.strip()]

        @property
        def pokes(self):
            if not poke_log.exists():
                return []
            return [l for l in poke_log.read_text().splitlines() if l.strip()]

        @property
        def turns(self):
            if not claude_log.exists():
                return []
            return [l for l in claude_log.read_text().splitlines() if l.startswith("turn ")]

        @property
        def turn_payloads(self):
            if not claude_log.exists():
                return []
            return [l for l in claude_log.read_text().splitlines() if l.startswith("{")]

        def set_known_good(self, version):
            self.state_file.write_text(json.dumps({"known_good": version}))

        @property
        def known_good(self):
            if not self.state_file.exists():
                return None
            return json.loads(self.state_file.read_text()).get("known_good")

    return World()


# =============================================================================
# The acceptance test, both halves
# =============================================================================

def test_a_known_bad_version_is_rolled_back_and_the_install_answers_again(world):
    """The issue's acceptance test end to end.

    Point the updater at a version whose CLI cannot complete a turn; the
    version that could must be back in place when it finishes.
    """
    world.break_version(BAD)
    result = world.run(UPGRADE, "--to", BAD)

    assert result.returncode == REVERTED, result.stdout + result.stderr
    assert world.installed == GOOD
    assert world.installs == [BAD, GOOD]


def test_the_channel_carries_a_notice_that_the_upgrade_was_reverted(world):
    """The other half of the acceptance test: the maintainer is told.

    Silent self-healing is how a fleet-wide bad release stays invisible — the
    install recovers and nobody ever learns the release is poison.
    """
    world.break_version(BAD)
    world.run(UPGRADE, "--to", BAD)

    assert len(world.notices) == 1
    notice = world.notices[0]
    assert "signals" in notice          # posted to a channel a human reads
    assert "revert" in notice.lower()
    assert BAD in notice and GOOD in notice


def test_the_notice_does_not_go_through_the_agent_queue(world):
    """poke.sh queues a message FOR AN AGENT.

    The thing being reported is that agents cannot complete a turn, so a
    notice routed through that queue lands where nobody can read it — the
    guard would fail silently in exactly the case it exists for. This is
    behavioural, not a grep: a poke.sh sitting right next to the notifier
    stays untouched.
    """
    world.break_version(BAD)
    world.run(UPGRADE, "--to", BAD)

    assert world.notices, "no notice was posted at all"
    assert world.pokes == []


# =============================================================================
# What counts as "verified"
# =============================================================================

def test_a_version_that_installs_and_reports_a_version_still_has_to_answer(world):
    """The whole point. `claude --version` answering proves nothing.

    Every other signal this system has — supervisord, /health, the port — is
    green during exactly this failure.
    """
    world.break_version(BAD)
    world.run(UPGRADE, "--to", BAD)

    # The fake CLI answered --version happily throughout; it was the turn
    # that failed, and the turn is what the decision was made on.
    assert world.turns, "no probe turn was ever attempted"
    assert world.installed == GOOD


def test_the_probe_uses_the_same_stream_json_wire_the_agent_server_does(world):
    """A release can break the protocol without breaking the binary.

    agent-server.py writes {"type":"user","message":{"role":..,"content":..}}
    to stdin and reads a `result` event back. If the probe used a different
    interface, a release that broke that envelope would sail through.
    """
    world.run(UPGRADE, "--verify-only")

    assert world.turn_payloads, "the probe sent nothing on stdin"
    envelope = json.loads(world.turn_payloads[0])
    assert envelope["type"] == "user"
    assert envelope["message"]["role"] == "user"
    assert isinstance(envelope["message"]["content"], str)


def test_a_cli_that_returns_no_result_event_fails_verification(world, tmp_path):
    """A turn that streams text and then dies mid-flight is not a completed
    turn. Accepting it would let a release that hangs every reply pass.
    """
    binp = tmp_path / "bin"
    write_exec(binp / "claude", """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "1.0.0 (Claude Code)"; exit 0; fi
cat > /dev/null
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"KARAKOS_CLI_OK"}]}}'
""")
    assert world.run(UPGRADE, "--verify-only").returncode != VERIFIED


def test_a_result_event_flagged_is_error_fails_verification(world, tmp_path):
    binp = tmp_path / "bin"
    write_exec(binp / "claude", """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "1.0.0 (Claude Code)"; exit 0; fi
cat > /dev/null
echo '{"type":"result","subtype":"error","is_error":true,"result":"KARAKOS_CLI_OK"}'
""")
    assert world.run(UPGRADE, "--verify-only").returncode != VERIFIED


def test_a_healthy_cli_passes_verification(world):
    """The positive control. Without it the guard could be failing every
    version for an unrelated reason and every test above would still be green.
    """
    assert world.run(UPGRADE, "--verify-only").returncode == VERIFIED


def test_a_working_upgrade_is_kept_and_nobody_is_paged(world):
    """A guard that reverts a good release is worse than no guard."""
    result = world.run(UPGRADE, "--to", "2.0.0")

    assert result.returncode == VERIFIED
    assert world.installed == "2.0.0"
    assert world.installs == ["2.0.0"]
    assert world.notices == []


def test_one_flaky_turn_does_not_revert_a_good_version(world, tmp_path):
    """A dropped API call is not a bad release.

    Reverting on the first failure would turn every transient network blip
    into an unnecessary downgrade, and operators switch off guards that do
    that.
    """
    binp = tmp_path / "bin"
    counter = tmp_path / "state" / "attempts"
    counter.write_text("")
    write_exec(binp / "claude", f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "--version" ]]; then echo "2.0.0 (Claude Code)"; exit 0; fi
cat > /dev/null
echo x >> "{counter}"
if [[ $(wc -l < "{counter}") -le 1 ]]; then
    echo "Error: connection reset" >&2
    exit 1
fi
echo '{{"type":"result","subtype":"success","is_error":false,"result":"KARAKOS_CLI_OK"}}'
""")
    result = world.run(UPGRADE, "--to", "2.0.0",
                       CLI_VERIFY_ATTEMPTS="2", CLI_VERIFY_RETRY_DELAY="0")

    assert result.returncode == VERIFIED
    assert world.installs == ["2.0.0"], "a retryable blip caused a revert"
    assert world.notices == []


# =============================================================================
# Failure modes of the guard itself
# =============================================================================

def test_an_upgrade_is_refused_when_there_is_no_rollback_target(world, tmp_path):
    """An upgrade with no known way back is the thing being guarded against.

    If the installed version cannot be read, performing the upgrade anyway
    would leave the install exactly one bad release from dark.
    """
    binp = tmp_path / "bin"
    write_exec(binp / "claude", '#!/usr/bin/env bash\nexit 127\n')

    result = world.run(UPGRADE, "--to", BAD)
    assert result.returncode == CANNOT_RUN
    assert world.installs == [], "an upgrade ran with nothing to roll back to"


def test_a_failed_revert_is_reported_louder_and_not_swallowed(world):
    """The worst case: broken CLI installed, revert impossible.

    This is the one state where the install genuinely is dark, and it must
    not exit 0 or stay quiet — the notice carries the manual command.
    """
    world.break_version(BAD)
    world.fail_npm_for(GOOD)

    result = world.run(UPGRADE, "--to", BAD)
    assert result.returncode == REVERT_FAILED
    assert len(world.notices) == 1
    assert "npm install -g @anthropic-ai/claude-code@1.0.0" in world.notices[0]


def test_a_revert_that_still_cannot_answer_says_so(world):
    """If the old version fails too, the upgrade probably was not the fault.

    Reporting "all fixed" here would send the operator hunting in the wrong
    place while messages keep going unanswered.
    """
    world.break_version(BAD)
    world.break_version(GOOD)

    result = world.run(UPGRADE, "--to", BAD)
    assert result.returncode == REVERTED
    assert world.installed == GOOD
    notice = world.notices[0]
    assert "revert" in notice.lower()
    assert "may not be the upgrade" in notice


def test_a_failed_install_leaves_the_working_version_alone(world):
    """A version that does not exist upstream is not an emergency."""
    world.fail_npm_for("3.0.0")

    result = world.run(UPGRADE, "--to", "3.0.0")
    assert result.returncode == CANNOT_RUN
    assert world.installed == GOOD
    assert world.notices == []


# =============================================================================
# The watchdog: upgrades that never went through our updater
# =============================================================================

def test_first_run_adopts_the_installed_version_without_spending_a_turn(world):
    """There is nothing to roll back to on a fresh install, so a probe would
    produce a finding with no action attached — and cost an API call on every
    new install to do it.
    """
    result = world.run(WATCHDOG)

    assert result.returncode == VERIFIED
    assert world.known_good == GOOD
    assert world.turns == [], "the first run spent an API turn for nothing"


def test_no_drift_means_no_api_turn(world):
    """Running hourly, this must not burn a turn an hour.

    Verifying unconditionally would also convert every API outage into a
    revert of a version that was never at fault.
    """
    world.set_known_good(GOOD)
    result = world.run(WATCHDOG)

    assert result.returncode == VERIFIED
    assert world.turns == []
    assert world.installs == []


def test_a_bad_cli_that_arrived_via_an_image_pull_is_rolled_back(world):
    """The path a real bad release travels: `docker compose pull` swaps the
    CLI underneath a running install, and our own updater is never involved.
    A rollback that only covers our updater covers the rarer half.
    """
    world.set_known_good(GOOD)
    world.break_version(BAD)
    world.installed = BAD          # as if the new image shipped it

    result = world.run(WATCHDOG)

    assert result.returncode == REVERTED, result.stdout + result.stderr
    assert world.installed == GOOD
    assert world.installs == [GOOD]
    assert world.notices and "revert" in world.notices[0].lower()
    assert world.pokes == []


def test_the_watchdog_does_not_adopt_a_version_that_failed(world):
    """Adopting it would make the broken version the rollback target, and the
    next drift would 'recover' onto it.
    """
    world.set_known_good(GOOD)
    world.break_version(BAD)
    world.installed = BAD

    world.run(WATCHDOG)
    assert world.known_good == GOOD


def test_a_good_new_cli_is_adopted_as_the_new_rollback_target(world):
    """Otherwise the guard would keep dragging the install back to an
    ever-older version every time a legitimate upgrade landed.
    """
    world.set_known_good(GOOD)
    world.installed = "2.0.0"

    result = world.run(WATCHDOG)
    assert result.returncode == VERIFIED
    assert world.installed == "2.0.0"
    assert world.known_good == "2.0.0"
    assert world.installs == []


@pytest.mark.parametrize("junk", ["{not json", "[]", "", '{"known_good": null}'])
def test_a_corrupt_known_good_record_does_not_trigger_a_revert(world, junk):
    """A truncated state file must degrade to 'adopt', not to reinstalling
    whatever garbage it parsed as a version.

    The load-bearing assertion is that no turn was spent: a parser that
    returned a bogus version would read as drift, and drift is what makes
    this script act. `installs == []` alone would not catch that, because a
    healthy CLI passes the probe and the bogus record gets quietly replaced.
    """
    world.state_file.write_text(junk)
    result = world.run(WATCHDOG)

    assert result.returncode == VERIFIED
    assert world.turns == [], "a corrupt record was read as a version and probed against"
    assert world.installs == []
    assert world.known_good == GOOD


def test_the_watchdog_reports_a_failed_revert_rather_than_exiting_clean(world):
    world.set_known_good(GOOD)
    world.break_version(BAD)
    world.installed = BAD
    world.fail_npm_for(GOOD)

    result = world.run(WATCHDOG)
    assert result.returncode == REVERT_FAILED
    assert world.notices and "also failed" in world.notices[0]


# =============================================================================
# The selftests — positive controls that ship with the scripts
# =============================================================================

@pytest.mark.parametrize("script", [UPGRADE, WATCHDOG], ids=["upgrade", "watchdog"])
def test_the_shipped_selftest_passes(script):
    """A rollback that has never fired is a rollback nobody knows is wired up.

    These run entirely on fakes, so an operator can confirm the guard is armed
    on a live install without breaking their CLI to find out.
    """
    result = subprocess.run(["bash", str(script), "--selftest"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert "FAIL" not in result.stdout


SABOTAGE = {
    UPGRADE.name: ('if ! npm_install "$previous"; then', "if ! true; then"),
    WATCHDOG.name: ('if ! bash "$UPGRADE_CLI" --install "$known_good"; then',
                    "if ! true; then"),
}


@pytest.mark.parametrize("script", [UPGRADE, WATCHDOG], ids=["upgrade", "watchdog"])
def test_the_selftest_would_fail_if_the_revert_stopped_happening(script, tmp_path):
    """The selftest's own negative control — a selftest that cannot fail is
    decoration.

    Copies both scripts, deletes the reinstall from the copy under test, and
    requires the shipped selftest to report FAIL. Without this, a selftest
    that had quietly stopped exercising the revert would keep printing PASS
    forever.
    """
    for src in (UPGRADE, WATCHDOG):
        dest = tmp_path / src.name
        dest.write_text(src.read_text())
        dest.chmod(0o755)

    victim = tmp_path / script.name
    old, new = SABOTAGE[script.name]
    text = victim.read_text()
    assert text.count(old) == 1, f"sabotage anchor moved in {script.name}"
    victim.write_text(text.replace(old, new))

    result = subprocess.run(["bash", str(victim), "--selftest"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert "FAIL" in result.stdout


# =============================================================================
# Wiring — a guard nothing runs is not a guard
# =============================================================================

def _function_names(tree):
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _string_literals_in(tree, func_name):
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == func_name)
    docstring = ast.get_docstring(node, clean=False)
    return [c.value for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
            and c.value != docstring]


def test_the_scheduler_actually_runs_the_watchdog():
    """Correct detection that nothing invokes is the same as no detection.

    Parsed rather than grepped: the scheduler's source discusses several
    scripts in comments, and a comment is not a call site.
    """
    tree = ast.parse(SCHEDULER.read_text())
    assert "run_cli_upgrade_watchdog" in _function_names(tree)

    literals = _string_literals_in(tree, "run_cli_upgrade_watchdog")
    assert any("cli-upgrade-watchdog.sh" in lit for lit in literals)

    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [c for c in ast.walk(main) if isinstance(c, ast.Call)]

    # Run once at startup. Startup is when a freshly pulled image has just
    # swapped the CLI out, so waiting for the first tick leaves the most
    # likely breakage unchallenged for an hour.
    assert any(isinstance(c.func, ast.Name)
               and c.func.id == "run_cli_upgrade_watchdog" for c in calls), \
        "the watchdog is never run at scheduler startup"

    # ...and on an hourly schedule thereafter: schedule.every().hour.do(fn)
    hourly = [
        c for c in calls
        if isinstance(c.func, ast.Attribute) and c.func.attr == "do"
        and isinstance(c.func.value, ast.Attribute) and c.func.value.attr == "hour"
        and c.args and isinstance(c.args[0], ast.Name)
        and c.args[0].id == "run_cli_upgrade_watchdog"
    ]
    assert hourly, "the watchdog is not on an hourly schedule"


def _shell_regions(path: Path):
    """(production code, selftest code) with comment lines dropped.

    Two exclusions, both deliberate. Comments go because both scripts explain
    at length *why* they do not use poke.sh, and a naive substring search
    reads that explanation as the thing it warns against. The selftest body
    goes because it deliberately plants a poke.sh stand-in as a negative
    control — the fixture for a check is not the behaviour under check.
    """
    production, selftest = [], []
    sink, depth = production, 0
    for line in path.read_text().splitlines():
        if line.strip().startswith("#"):
            continue
        if line.startswith("run_selftest() {"):
            sink, depth = selftest, 1
            continue
        if sink is selftest and line == "}":
            sink = production
            continue
        sink.append(line)
    return "\n".join(production), "\n".join(selftest)


@pytest.mark.parametrize("script", [UPGRADE, WATCHDOG], ids=["upgrade", "watchdog"])
def test_no_production_line_reaches_for_poke_sh(script):
    """Belt and braces on the behavioural test above: not only did poke.sh go
    untouched at runtime, no production code path names it at all.
    """
    production, selftest = _shell_regions(script)
    assert "poke.sh" not in production
    assert "discord-notify.sh" in production
    # And the negative control is still planted, so the runtime assertion
    # "poke.sh was not called" is checking something that exists.
    assert "poke.sh" in selftest


@pytest.mark.parametrize("script", [UPGRADE, WATCHDOG], ids=["upgrade", "watchdog"])
def test_the_scripts_are_executable(script):
    assert script.exists()
    assert script.stat().st_mode & stat.S_IXUSR
