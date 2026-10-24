"""workspace_dirty: startup report of uncommitted workspace files."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import workspace_dirty  # noqa: E402
from workspace_dirty import describe, dirty_files  # noqa: E402


def test_no_git_dir_returns_empty(tmp_path):
    assert dirty_files(tmp_path) == []


def test_clean_repo_returns_empty(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert dirty_files(tmp_path) == []


def test_untracked_and_modified_files_are_named(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("one\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "x"], check=True)
    (tmp_path / "a.py").write_text("two\n")
    (tmp_path / "b.py").write_text("new\n")
    assert sorted(dirty_files(tmp_path)) == ["a.py", "b.py"]


def test_describe_names_files_and_crash():
    msg = describe(["x.py"], True)
    assert "previous run crashed" in msg
    assert "x.py" in msg
    assert describe([], True) == ""


def test_describe_truncates():
    msg = describe([f"f{i}" for i in range(25)], False)
    assert "(+5 more)" in msg
    assert "f24" not in msg
