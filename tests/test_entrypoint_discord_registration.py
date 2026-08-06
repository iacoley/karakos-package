"""
Tests for the Discord slash-command registration step in bin/entrypoint.sh
(issue #87) — registration must run automatically on container start with
no documented follow-up step, must be skipped when Discord isn't
configured at all, and must never block the rest of startup on failure.

A fake `register-discord-commands.py` (records that it ran) and a fake
`supervisord` on PATH (records that startup reached the end) stand in for
the real ones, so this exercises the actual bash control flow without
Docker, a container, or a live Discord guild.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
ENTRYPOINT = PACKAGE_ROOT / "bin" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="volume guard reads as writable when running as root"
)

DISCORD_ENV = {
    "DISCORD_BOT_TOKEN_PRIMARY": "test-token",
    "DISCORD_BOT_ID_PRIMARY": "111222333",
    "DISCORD_SERVER_ID": "444555666",
}


def _make_executable(path: Path, script_body: str):
    path.write_text(script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def make_workspace(tmp_path: Path, registration_exit: int = 0) -> Path:
    for name in ("data", "logs", "inbox", "bin"):
        (tmp_path / name).mkdir()

    # entrypoint.sh invokes this with `python3 <path>`, same as every other
    # bin/*.py script in supervisord.conf — so the stub has to be real
    # Python, not a shell script with a shebang python3 would ignore.
    marker = tmp_path / "registration-ran.marker"
    _make_executable(
        tmp_path / "bin" / "register-discord-commands.py",
        f"import pathlib, sys\npathlib.Path(r'{marker}').touch()\nsys.exit({registration_exit})\n",
    )
    return tmp_path


def run_entrypoint(workspace: Path, extra_env=None) -> subprocess.CompletedProcess:
    # A fake supervisord on PATH so `exec supervisord ...` at the end of the
    # real script succeeds instead of failing with "command not found",
    # which would make every case here look identical.
    fake_bin = workspace / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    supervisord_marker = workspace / "supervisord-reached.marker"
    _make_executable(
        fake_bin / "supervisord",
        f"#!/usr/bin/env bash\ntouch '{supervisord_marker}'\nexit 0\n",
    )

    env = {
        **os.environ,
        "WORKSPACE_ROOT": str(workspace),
        "DASHBOARD_PORT": "3000",
        "AGENT_SERVER_TOKEN": "test-token",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result.supervisord_reached = supervisord_marker.exists()
    return result


def test_skipped_when_discord_is_not_configured(tmp_path):
    workspace = make_workspace(tmp_path)
    marker = workspace / "registration-ran.marker"

    result = run_entrypoint(workspace)

    assert not marker.exists(), "registration ran with no Discord config present"
    assert result.supervisord_reached


def test_skipped_when_discord_is_only_partially_configured(tmp_path):
    workspace = make_workspace(tmp_path)
    marker = workspace / "registration-ran.marker"
    partial_env = {"DISCORD_BOT_TOKEN_PRIMARY": "test-token"}  # missing id + server id

    result = run_entrypoint(workspace, partial_env)

    assert not marker.exists()
    assert result.supervisord_reached


def test_runs_automatically_when_discord_is_configured(tmp_path):
    workspace = make_workspace(tmp_path)
    marker = workspace / "registration-ran.marker"

    result = run_entrypoint(workspace, DISCORD_ENV)

    assert marker.exists(), "entrypoint did not invoke slash-command registration"
    assert result.supervisord_reached


def test_registration_failure_does_not_block_startup(tmp_path):
    workspace = make_workspace(tmp_path, registration_exit=1)

    result = run_entrypoint(workspace, DISCORD_ENV)

    assert "WARNING" in result.stderr
    assert "slash-command registration failed" in result.stderr
    assert result.supervisord_reached, "a registration failure must not abort startup"
