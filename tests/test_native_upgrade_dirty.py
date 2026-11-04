"""bin/native-upgrade.sh: the per-install .karakos/config.json must not block an upgrade."""
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "native-upgrade.sh"


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _dirty_spec():
    m = re.search(r"DIRTY_SPEC=\((.*)\)", SCRIPT.read_text())
    assert m, "native-upgrade.sh no longer defines DIRTY_SPEC"
    return subprocess.run(["bash", "-c", f"printf '%s\\n' {m.group(1)}"],
                          capture_output=True, text=True, check=True).stdout.split("\n")[:-1]


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".karakos").mkdir()
    (tmp_path / ".karakos" / "config.json").write_text('{"owner_name": "User"}')
    (tmp_path / "other.txt").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")


def test_setup_written_config_is_not_dirty(tmp_path):
    _repo(tmp_path)
    (tmp_path / ".karakos" / "config.json").write_text('{"owner_name": "Someone"}')
    out = _git(tmp_path, "status", "--porcelain", "--untracked-files=no", *_dirty_spec())
    assert out == ""


def test_other_tracked_change_is_still_dirty(tmp_path):
    _repo(tmp_path)
    (tmp_path / ".karakos" / "config.json").write_text('{"owner_name": "Someone"}')
    (tmp_path / "other.txt").write_text("changed")
    out = _git(tmp_path, "status", "--porcelain", "--untracked-files=no", *_dirty_spec())
    assert "other.txt" in out and "config.json" not in out
