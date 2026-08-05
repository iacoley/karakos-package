"""
Tests for the persistent-volume writability guard in bin/entrypoint.sh

Strategy:
  - Point WORKSPACE_ROOT at a tmp directory so each test controls the
    permissions of data/, logs/ and inbox/ without touching the real repo.
  - Run the script with subprocess and assert exit code / stderr.

Only the guard is exercised: it runs before any of the container-specific
setup, so the script exits on the failure path without needing docker, git,
or a populated /workspace.
"""

import os
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
ENTRYPOINT = PACKAGE_ROOT / "bin" / "entrypoint.sh"

# Root ignores the mode bits the guard relies on, so it would never trip.
pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="guard tests read as writable when running as root"
)

VOLUME_DIRS = ("data", "logs", "inbox")


def make_workspace(tmp_path: Path) -> Path:
    """A workspace whose three persistent-volume roots are all writable."""
    for name in VOLUME_DIRS:
        (tmp_path / name).mkdir()
    return tmp_path


def run_entrypoint(workspace: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "WORKSPACE_ROOT": str(workspace),
        "DASHBOARD_PORT": "3000",
        "AGENT_SERVER_TOKEN": "test-token",
    }
    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("unwritable", VOLUME_DIRS)
def test_unwritable_volume_root_aborts_with_a_readable_error(tmp_path, unwritable):
    workspace = make_workspace(tmp_path)
    target = workspace / unwritable
    target.chmod(0o555)
    try:
        result = run_entrypoint(workspace)
    finally:
        target.chmod(0o755)

    assert result.returncode == 1
    assert "cannot write to its own storage" in result.stderr
    assert str(target) in result.stderr
    # The message has to name the actual remedy, not just the symptom — the
    # audience is a non-developer reading container output.
    assert "docker compose down -v" in result.stderr


def test_all_unwritable_volumes_are_listed_in_one_pass(tmp_path):
    workspace = make_workspace(tmp_path)
    targets = [workspace / name for name in VOLUME_DIRS]
    for target in targets:
        target.chmod(0o555)
    try:
        result = run_entrypoint(workspace)
    finally:
        for target in targets:
            target.chmod(0o755)

    assert result.returncode == 1
    for target in targets:
        assert str(target) in result.stderr


def test_writable_volumes_pass_the_guard(tmp_path):
    """The guard must not be the thing that stops a healthy workspace."""
    workspace = make_workspace(tmp_path)
    result = run_entrypoint(workspace)

    assert "cannot write to its own storage" not in result.stderr


def test_missing_volume_dirs_pass_the_guard(tmp_path):
    """A first-run workspace has no volume dirs yet; mkdir -p creates them."""
    result = run_entrypoint(tmp_path)

    assert "cannot write to its own storage" not in result.stderr
    for name in VOLUME_DIRS:
        assert (tmp_path / name).is_dir()
