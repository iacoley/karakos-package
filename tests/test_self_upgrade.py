"""
Tests for the Docker self-upgrade: bin/self-upgrade.sh (container side) and
bin/upgrade-agent.sh (host side).

Both are the real scripts run as subprocesses against a temp tree. `docker`,
`make` and `git` are recording stand-ins on PATH, as is `bin/poke.sh`, so a
test fails on what was actually called, not on a mock mismatch.
"""

import fcntl
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
SELF_UPGRADE = PACKAGE_ROOT / "bin" / "self-upgrade.sh"
UPGRADE_AGENT = PACKAGE_ROOT / "bin" / "upgrade-agent.sh"
PATHS_SH = PACKAGE_ROOT / "bin" / "lib" / "paths.sh"

JSON_HEALTHY = [{"Name": "karakos", "State": "running", "Health": "healthy", "ExitCode": 0}]
JSON_STARTING = [{"Name": "karakos", "State": "running", "Health": "starting", "ExitCode": 0}]
JSON_EXIT_78 = [{"Name": "karakos", "State": "restarting", "Health": "", "ExitCode": 78}]


def write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def calls(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


# --------------------------------------------------------------------------
# Container side: bin/self-upgrade.sh
# --------------------------------------------------------------------------


class Container:
    def __init__(self, root: Path):
        self.root = root
        self.upgrade = root / ".karakos" / "upgrade"
        self.pokes = root / "pokes.log"
        write_exec(root / "bin" / "self-upgrade.sh", SELF_UPGRADE.read_text())
        (root / "bin" / "lib").mkdir()
        shutil.copy(PATHS_SH, root / "bin" / "lib" / "paths.sh")
        write_exec(
            root / "bin" / "poke.sh",
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{self.pokes}"\n',
        )

    def run(self, *args: str, agent: str | None = "amos") -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "WORKSPACE_ROOT": str(self.root),
               "KARAKOS_UPGRADE_REPORT_WAIT": "0"}
        if agent:
            env["KARAKOS_AGENT_NAME"] = agent
        return subprocess.run(
            ["bash", str(self.root / "bin" / "self-upgrade.sh"), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def heartbeat(self) -> None:
        self.upgrade.mkdir(parents=True, exist_ok=True)
        (self.upgrade / "host-agent.seen").touch()


@pytest.fixture
def container(tmp_path):
    return Container(tmp_path)


def test_request_writes_both_files(container):
    container.heartbeat()
    r = container.run("request", "v2.5.0")
    assert r.returncode == 0, r.stderr
    req = read_json(container.upgrade / "request.json")
    assert req["version"] == "v2.5.0"
    assert req["requested_by"] == "amos"
    assert req["requested_at"].endswith("Z")
    st = read_json(container.upgrade / "status.json")
    assert st["state"] == "pending"
    assert st["reported"] is False


def test_request_defaults_to_latest(container):
    container.heartbeat()
    assert container.run("request").returncode == 0
    assert read_json(container.upgrade / "request.json")["version"] == "latest"


def test_requested_by_falls_back_to_unknown(container):
    container.heartbeat()
    env_run = subprocess.run(
        ["bash", str(container.root / "bin" / "self-upgrade.sh"), "request", "latest"],
        env={"PATH": "/usr/bin:/bin", "WORKSPACE_ROOT": str(container.root)},
        capture_output=True, text=True,
    )
    assert env_run.returncode == 0, env_run.stderr
    assert read_json(container.upgrade / "request.json")["requested_by"] == "unknown"


@pytest.mark.parametrize("bad", ["2.5.0", "v2", "v2.5.0.1", "main", "v2.5.0; rm -rf /", "latest "])
def test_bad_version_exits_2_and_writes_nothing(container, bad):
    container.heartbeat()
    r = container.run("request", bad)
    assert r.returncode == 2
    assert not (container.upgrade / "request.json").exists()
    assert not (container.upgrade / "status.json").exists()


@pytest.mark.parametrize("state", ["pending", "running"])
def test_second_request_while_in_flight_exits_3(container, state):
    container.heartbeat()
    assert container.run("request", "v2.5.0").returncode == 0
    if state == "running":
        st = read_json(container.upgrade / "status.json")
        st["state"] = "running"
        (container.upgrade / "status.json").write_text(json.dumps(st))
    before = (container.upgrade / "request.json").read_text()
    r = container.run("request", "v2.6.0")
    assert r.returncode == 3
    assert (container.upgrade / "request.json").read_text() == before


def test_request_after_finished_upgrade_is_allowed(container):
    container.heartbeat()
    container.run("request", "v2.5.0")
    st = read_json(container.upgrade / "status.json")
    st["state"] = "done"
    (container.upgrade / "status.json").write_text(json.dumps(st))
    assert container.run("request", "v2.6.0").returncode == 0
    assert read_json(container.upgrade / "status.json")["state"] == "pending"


def test_missing_heartbeat_exits_4_but_keeps_request(container):
    r = container.run("request", "v2.5.0")
    assert r.returncode == 4
    assert "host upgrade agent not detected; run `make install-upgrade-agent` on the host once" in r.stderr
    assert (container.upgrade / "request.json").exists()


def test_stale_heartbeat_exits_4(container):
    container.heartbeat()
    old = (container.upgrade / "host-agent.seen")
    os.utime(old, (1, 1))
    assert container.run("request", "v2.5.0").returncode == 4


def test_status_without_request(container):
    r = container.run("status")
    assert r.returncode == 0
    assert "no upgrade requested" in r.stdout


def test_status_prints_status_json(container):
    container.heartbeat()
    container.run("request", "v2.5.0")
    assert json.loads(container.run("status").stdout)["state"] == "pending"


def finished(container, state="done", **extra):
    container.upgrade.mkdir(parents=True, exist_ok=True)
    st = {"state": state, "version_from": "v2.4.0", "version_to": "v2.5.0",
          "started_at": "x", "finished_at": "y", "detail": "ok", "reported": False,
          "requested_by": "amos"}
    st.update(extra)
    (container.upgrade / "status.json").write_text(json.dumps(st))


def test_report_pokes_once_and_marks_reported(container):
    finished(container)
    assert container.run("report").returncode == 0
    lines = calls(container.pokes)
    assert len(lines) == 1
    assert "--agent amos" in lines[0] and "v2.4.0" in lines[0] and "v2.5.0" in lines[0]
    assert read_json(container.upgrade / "status.json")["reported"] is True

    assert container.run("report").returncode == 0
    assert len(calls(container.pokes)) == 1


@pytest.mark.parametrize("state", ["failed", "needs-migrate"])
def test_report_covers_failure_states(container, state):
    finished(container, state=state)
    container.run("report")
    assert len(calls(container.pokes)) == 1
    assert read_json(container.upgrade / "status.json")["reported"] is True


@pytest.mark.parametrize("state", ["pending", "running"])
def test_report_ignores_in_flight(container, state):
    finished(container, state=state)
    assert container.run("report").returncode == 0
    assert calls(container.pokes) == []
    assert read_json(container.upgrade / "status.json")["reported"] is False


def test_report_waits_for_a_running_upgrade_to_finish(container):
    """The container boots mid-upgrade; the host flips running -> done after its health check."""
    finished(container, state="running")
    status = container.upgrade / "status.json"

    def flip():
        time.sleep(1)
        st = read_json(status)
        st["state"] = "done"
        status.write_text(json.dumps(st))

    t = threading.Thread(target=flip)
    t.start()
    env = {"PATH": "/usr/bin:/bin", "WORKSPACE_ROOT": str(container.root),
           "KARAKOS_UPGRADE_REPORT_WAIT": "30"}
    r = subprocess.run(["bash", str(container.root / "bin" / "self-upgrade.sh"), "report"],
                       env=env, capture_output=True, text=True, timeout=60)
    t.join()
    assert r.returncode == 0
    assert len(calls(container.pokes)) == 1
    assert read_json(status)["reported"] is True


def test_report_never_fails(container):
    assert container.run("report").returncode == 0  # no status.json at all
    finished(container)
    (container.root / "bin" / "poke.sh").unlink()
    assert container.run("report").returncode == 0
    assert read_json(container.upgrade / "status.json")["reported"] is False
    (container.upgrade / "status.json").write_text("not json")
    assert container.run("report").returncode == 0


def test_report_falls_back_to_primary_when_requester_is_not_an_agent(container):
    finished(container, requested_by="nobody")
    write_exec(
        container.root / "bin" / "poke.sh",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{container.pokes}"\n'
        '[[ "$*" == *--agent* ]] && exit 1\nexit 0\n',
    )
    container.run("report")
    lines = calls(container.pokes)
    assert len(lines) == 2 and "--agent" not in lines[1]
    assert read_json(container.upgrade / "status.json")["reported"] is True


# --------------------------------------------------------------------------
# Host side: bin/upgrade-agent.sh
# --------------------------------------------------------------------------


class Host:
    def __init__(self, root: Path):
        self.root = root
        self.upgrade = root / ".karakos" / "upgrade"
        self.upgrade.mkdir(parents=True)
        self.calls = root / "calls.log"
        self.ps = root / "ps.json"
        self.stubs = root / "stubs"
        (root / "config").mkdir()
        self.env_file = root / "config" / ".env"
        self.env_file.write_text("FOO=bar\nKARAKOS_VERSION=v2.4.0\nBAZ=1\n")
        write_exec(root / "bin" / "upgrade-agent.sh", UPGRADE_AGENT.read_text())
        self.ps.write_text(json.dumps(JSON_HEALTHY))
        # Recording stand-ins. STUB_FAIL = "<name>" or "<name> <arg>" makes that call exit 1.
        record = 'echo "NAME $*" >> "' + str(self.calls) + '"\n'
        fail = '[[ "${STUB_FAIL:-}" == "NAME $1" || "${STUB_FAIL:-}" == "NAME" ]] && exit 1\n'
        for name in ("make", "git"):
            write_exec(self.stubs / name, "#!/usr/bin/env bash\n" + record.replace("NAME", name)
                       + fail.replace("NAME", name) + "exit 0\n")
        write_exec(
            self.stubs / "docker",
            "#!/usr/bin/env bash\n" + record.replace("NAME", "docker")
            + '[[ "$1" == compose && " $* " == *" ps "* ]] && { cat "' + str(self.ps) + '"; exit 0; }\n'
            + '[[ "${STUB_FAIL:-}" == "docker image" && "$1" == image ]] && exit 1\n'
            + "exit 0\n",
        )

    def request(self, version="v2.5.0", state="pending", write_status=True):
        (self.upgrade / "request.json").write_text(json.dumps(
            {"version": version, "requested_by": "amos", "requested_at": "2026-11-02T00:00:00Z"}))
        if write_status:
            (self.upgrade / "status.json").write_text(json.dumps(
                {"state": state, "version_from": "", "version_to": version, "started_at": "",
                 "finished_at": "", "detail": "", "reported": False, "requested_by": "amos"}))

    def run(self, **env_extra) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "HOME": str(self.root),
            "KARAKOS_UPGRADE_HEALTH_TIMEOUT": "0",
            "KARAKOS_UPGRADE_HEALTH_INTERVAL": "0",
            **env_extra,
        }
        return subprocess.run(
            ["bash", str(self.root / "bin" / "upgrade-agent.sh")],
            env=env, capture_output=True, text=True, timeout=60,
        )

    @property
    def status(self) -> dict:
        return read_json(self.upgrade / "status.json")

    @property
    def pin(self) -> list[str]:
        return [l for l in self.env_file.read_text().splitlines() if l.startswith("KARAKOS_VERSION")]

    def all_calls(self) -> list[str]:
        return calls(self.calls)

    def make_calls(self) -> list[str]:
        return [c for c in self.all_calls() if c.startswith("make ")]


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_agent_without_request_only_touches_heartbeat(host):
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert (host.upgrade / "host-agent.seen").exists()
    assert host.all_calls() == []
    assert not (host.upgrade / "status.json").exists()
    assert not (host.upgrade / "host.log").exists()


def test_agent_ignores_request_that_is_not_pending(host):
    host.request(state="done")
    assert host.run().returncode == 0
    assert host.all_calls() == []
    assert (host.upgrade / "request.json").exists()


def test_agent_rejects_invalid_version(host):
    host.request(version="v2.5.0; reboot")
    assert host.run().returncode == 0
    assert host.status["state"] == "failed"
    assert host.status["detail"] == "invalid version"
    assert not (host.upgrade / "request.json").exists()
    assert host.all_calls() == []


def test_success_pins_version_and_writes_done(host):
    host.request("v2.5.0")
    r = host.run()
    assert r.returncode == 0, r.stderr
    st = host.status
    assert st["state"] == "done"
    assert st["version_from"] == "v2.4.0"
    assert st["version_to"] == "v2.5.0"
    assert st["started_at"] and st["finished_at"]
    assert st["reported"] is False and st["requested_by"] == "amos"
    assert host.pin == ["KARAKOS_VERSION=v2.5.0"]
    assert "FOO=bar" in host.env_file.read_text() and "BAZ=1" in host.env_file.read_text()
    assert not (host.upgrade / "request.json").exists()
    assert host.make_calls() == ["make pull", "make down", "make up"]
    cs = host.all_calls()
    assert "docker image tag ghcr.io/mcarmody/karakos:v2.4.0 ghcr.io/mcarmody/karakos:rollback" in cs
    assert cs.index("git pull --ff-only origin main") < cs.index("make pull")
    assert "git pull --ff-only origin main" in (host.upgrade / "host.log").read_text()
    assert (host.upgrade / "host.log").exists()


def test_pin_is_appended_when_absent(host):
    host.env_file.write_text("FOO=bar\n")
    host.request("v2.5.0")
    host.run()
    assert host.status["version_from"] == "latest"
    assert host.pin == ["KARAKOS_VERSION=v2.5.0"]
    assert host.env_file.read_text().startswith("FOO=bar\n")


def test_latest_leaves_existing_pin_alone_and_says_so(host):
    host.request("latest")
    host.run()
    st = host.status
    assert st["state"] == "done"
    assert host.pin == ["KARAKOS_VERSION=v2.4.0"]
    assert "pinned" in st["detail"] and "v2.4.0" in st["detail"]


def test_health_failure_rolls_back_and_writes_failed(host):
    host.ps.write_text(json.dumps(JSON_STARTING))
    host.request("v2.5.0")
    r = host.run()
    assert r.returncode == 0, r.stderr
    st = host.status
    assert st["state"] == "failed"
    assert "health check" in st["detail"] and "v2.4.0" in st["detail"]
    assert host.pin == ["KARAKOS_VERSION=rollback"]
    # pull, down, up for the upgrade, then up again for the rollback
    assert host.make_calls() == ["make pull", "make down", "make up", "make up"]
    assert not (host.upgrade / "request.json").exists()


def test_make_step_failure_names_the_step_and_rolls_back(host):
    host.request("v2.5.0")
    host.run(STUB_FAIL="make pull")
    st = host.status
    assert st["state"] == "failed" and "make pull" in st["detail"]
    assert host.pin == ["KARAKOS_VERSION=rollback"]
    assert "make down" not in host.make_calls()
    assert host.make_calls()[-1] == "make up"


def test_exit_78_is_needs_migrate_and_never_migrates(host):
    host.ps.write_text(json.dumps(JSON_EXIT_78))
    host.request("v2.5.0")
    host.run()
    st = host.status
    assert st["state"] == "needs-migrate"
    assert "karakos migrate" in st["detail"]
    assert host.pin == ["KARAKOS_VERSION=rollback"]
    assert not any("migrate" in c for c in host.all_calls())
    assert host.make_calls() == ["make pull", "make down", "make up", "make up"]


def test_ps_output_as_ndjson_is_understood(host):
    host.ps.write_text(json.dumps(JSON_HEALTHY[0]) + "\n")
    host.request("v2.5.0")
    host.run()
    assert host.status["state"] == "done"


def test_git_pull_failure_runs_no_make_steps(host):
    host.request("v2.5.0")
    host.run(STUB_FAIL="git")
    st = host.status
    assert st["state"] == "failed" and "git pull" in st["detail"]
    assert host.make_calls() == []
    assert host.pin == ["KARAKOS_VERSION=v2.4.0"]  # untouched
    assert not (host.upgrade / "request.json").exists()


def test_rollback_tag_failure_changes_nothing(host):
    host.request("v2.5.0")
    host.run(STUB_FAIL="docker image")
    assert host.status["state"] == "failed"
    assert not any(c.startswith("git ") for c in host.all_calls())
    assert host.make_calls() == []


def test_run_with_lock_held_does_nothing(host):
    host.request("v2.5.0")
    with open(host.upgrade / "host-agent.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = host.run()
    assert r.returncode == 0
    assert host.all_calls() == []
    assert host.status["state"] == "pending"
    assert (host.upgrade / "request.json").exists()


def test_stale_running_state_is_marked_failed(host):
    host.request("v2.5.0", state="running")
    host.run()
    assert host.status["state"] == "failed"
    assert "interrupted" in host.status["detail"]
    assert host.all_calls() == []
    assert not (host.upgrade / "request.json").exists()


def test_scripts_are_valid_bash():
    for script in (SELF_UPGRADE, UPGRADE_AGENT):
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
