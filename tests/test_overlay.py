"""Tests for the private overlay repo (#243, "MÍRDAIN G12: a private overlay repo for per-install data"). Temp dirs only, no network.

Clone and remote tests use a local bare repository path, never a URL.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "overlay.py"
sys.path.insert(0, str(ROOT))

from lib import overlay as ov  # noqa: E402
from lib.migrate import backup  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    monkeypatch.delenv("KARAKOS_OVERLAY_REPO", raising=False)
    monkeypatch.setenv("KARAKOS_SECRET_SCAN", "regex")  # no gitleaks: deterministic and fast
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "Overlay Test")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "overlay-test@example.invalid")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def cli(*args, ws=None):
    cmd = [sys.executable, str(BIN), *args]
    if ws is not None:
        cmd += ["--workspace", str(ws)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def init_overlay(ws: Path) -> Path:
    r = cli("init", ws=ws)
    assert r.returncode == 0, r.stderr
    return ws / "overlay"


def test_init_creates_overlay_repo_and_is_a_noop_the_second_time(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    ovd = ws / "overlay"
    r = cli("init", ws=ws)
    assert r.returncode == 0, r.stderr
    assert (ovd / ".git").is_dir()
    gi = (ovd / ".gitignore").read_text()
    for pattern in (".env*", "*.pem", "*.key", "secrets/"):
        assert pattern in gi
    assert (ovd / "secrets.manifest").read_text().startswith("#")
    assert (ovd / "README.md").is_file()
    hook = ovd / ".git" / "hooks" / "pre-commit"
    assert hook.is_file() and os.access(hook, os.X_OK)
    assert str(ROOT / "system" / "check-secrets.py") in hook.read_text()
    assert git(ovd, "log", "-1", "--format=%s").stdout.strip() == "Initial overlay"
    assert git(ovd, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"

    again = cli("init", ws=ws)
    assert again.returncode == 0
    assert "overlay already initialised" in again.stdout
    assert git(ovd, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_init_with_remote_on_an_empty_overlay_dir_sets_origin(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    (ws / "overlay").mkdir()
    r = cli("init", "--remote", str(bare), ws=ws)
    assert r.returncode == 0, r.stderr
    assert git(ws / "overlay", "remote", "get-url", "origin").stdout.strip() == str(bare)


def test_init_clones_a_configured_remote_when_the_dir_is_absent(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    write(ws / "config" / ".env", f'KARAKOS_OVERLAY_REPO="{bare}"\n')
    r = cli("init", ws=ws)
    assert r.returncode == 0, r.stderr
    ovd = ws / "overlay"
    assert (ovd / ".git").is_dir()
    hook = ovd / ".git" / "hooks" / "pre-commit"
    assert hook.is_file() and os.access(hook, os.X_OK), "clone path must install the guard too"


def test_remote_is_read_from_env_or_parsed_from_env_file_never_sourced(tmp_path):
    write(tmp_path / "config" / ".env", "export KARAKOS_OVERLAY_REPO='git@example.invalid:me/o.git'\n")
    assert ov.configured_remote(tmp_path, environ={}) == "git@example.invalid:me/o.git"
    assert ov.configured_remote(tmp_path, environ={"KARAKOS_OVERLAY_REPO": "/x.git"}) == "/x.git"
    write(tmp_path / "config" / ".env", "KARAKOS_OVERLAY_REPO=$(touch /should-not-run)\n")
    assert ov.configured_remote(tmp_path, environ={}) == "$(touch /should-not-run)"


def test_apply_copies_seam_file_and_reports_unchanged_on_second_run(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    ovd = init_overlay(ws)
    write(ovd / "config" / "agents.yaml", "agents: {}\n")
    plan = ov.apply(ws)
    assert ("copy", "config/agents.yaml") in plan
    assert (ws / "config" / "agents.yaml").read_text() == "agents: {}\n"
    assert ("unchanged", "config/agents.yaml") in ov.apply(ws)

    r = cli("apply", ws=ws)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""  # unchanged files are not printed
    r = cli("apply", "--quiet", ws=ws)
    assert r.returncode == 0 and r.stdout == ""


def test_apply_skips_package_tracked_paths(tmp_path):
    ws = tmp_path / "install"
    write(ws / "agents" / "CORE.md", "package core\n")
    write(ws / "agents" / "main" / "SYSTEM_PROMPT.md", "package prompt\n")
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    git(ws, "add", "agents/CORE.md", "agents/main/SYSTEM_PROMPT.md")
    ovd = init_overlay(ws)
    write(ovd / "agents" / "CORE.md", "overlay core\n")
    write(ovd / "agents" / "main" / "SYSTEM_PROMPT.md", "overlay prompt\n")

    plan = ov.apply(ws)
    assert ("skip-tracked", "agents/main/SYSTEM_PROMPT.md") in plan
    # CORE.md is never a seam (files under agents/ are not walked), so it is never copied.
    assert all(rel != "agents/CORE.md" for _, rel in plan)
    assert (ws / "agents" / "main" / "SYSTEM_PROMPT.md").read_text() == "package prompt\n"
    assert (ws / "agents" / "CORE.md").read_text() == "package core\n"


def test_capture_copies_new_seams_and_never_templates(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    ovd = init_overlay(ws)
    write(ws / "agents" / "foo" / "SYSTEM_PROMPT.md", "foo prompt\n")
    write(ws / "skills" / "bar" / "SKILL.md", "bar skill\n")
    write(ws / "agents" / "templates" / "x.md", "template\n")
    write(ws / "agents" / "foo" / "journal" / "2026-10-19.md", "journal\n")
    write(ws / "agents" / "foo" / "SYSTEM_PROMPT.generated.md", "composed\n")

    plan = ov.capture(ws)
    assert ("copy", "agents/foo/SYSTEM_PROMPT.md") in plan
    assert (ovd / "agents" / "foo" / "SYSTEM_PROMPT.md").read_text() == "foo prompt\n"
    assert (ovd / "skills" / "bar" / "SKILL.md").read_text() == "bar skill\n"
    assert not (ovd / "agents" / "templates" / "x.md").exists()
    assert not (ovd / "agents" / "foo" / "journal").exists()
    assert not (ovd / "agents" / "foo" / "SYSTEM_PROMPT.generated.md").exists()

    r = cli("capture", ws=ws)
    assert r.returncode == 0, r.stderr
    assert git(ovd, "log", "-1", "--format=%s").stdout.strip() == "Capture install state"
    again = cli("capture", ws=ws)
    assert "nothing to capture" in again.stdout


def test_package_shipped_skill_is_never_captured_or_overwritten(tmp_path):
    ws = tmp_path / "install"
    write(ws / "skills" / "hello" / "SKILL.md", "package skill\n")
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    git(ws, "add", "skills/hello/SKILL.md")
    ovd = init_overlay(ws)
    write(ovd / "skills" / "hello" / "extra.md", "overlay extra\n")
    write(ovd / "skills" / "mine" / "SKILL.md", "mine\n")

    plan = ov.apply(ws)
    assert ("skip-tracked", "skills/hello/extra.md") in plan
    assert ("copy", "skills/mine/SKILL.md") in plan
    assert not (ws / "skills" / "hello" / "extra.md").exists()

    write(ws / "skills" / "hello" / "SKILL.md", "package skill\n")
    cap = ov.capture(ws)
    assert ("skip-tracked", "skills/hello/SKILL.md") in cap
    assert not (ovd / "skills" / "hello" / "SKILL.md").exists()


def test_seam_listing_skips_excluded_names_and_secrets(tmp_path):
    write(tmp_path / "agents" / "templates" / "primary.md", "t\n")
    write(tmp_path / "agents" / "CORE.md", "c\n")
    write(tmp_path / "agents" / "foo" / "SYSTEM_PROMPT.md", "p\n")
    write(tmp_path / "agents" / "foo" / "__pycache__" / "x.pyc", "b\n")
    write(tmp_path / "agents" / "foo" / "inbox" / "m.md", "m\n")
    write(tmp_path / "agents" / "foo" / "SYSTEM_PROMPT.generated.md", "g\n")
    write(tmp_path / "agents" / "foo" / ".env", "TOKEN=x\n")
    write(tmp_path / "config" / "channels.json", "{}\n")
    write(tmp_path / "secrets.manifest", "X\n")
    listed = list(ov.list_seam_files(tmp_path))
    assert listed == ["config/channels.json", "agents/foo/SYSTEM_PROMPT.md"]


def test_pre_commit_hook_rejects_a_staged_env_file(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    ovd = init_overlay(ws)
    write(ovd / ".env", "API_TOKEN=not-a-real-value\n")
    git(ovd, "add", "-f", ".env")  # .gitignore would refuse a plain add
    r = git(ovd, "commit", "-m", "try to commit an env file")
    assert r.returncode != 0
    assert "environment file" in r.stderr and "check-secrets" in r.stderr
    assert git(ovd, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_check_secrets_flags_forbidden_and_secret_looking_tracked_files(tmp_path):
    ws = tmp_path / "install"
    ws.mkdir()
    ovd = init_overlay(ws)
    assert cli("check-secrets", ws=ws).returncode == 0
    write(ovd / "agents" / "foo" / "note.md", "token ghp_" + "a" * 36 + "\n")
    git(ovd, "add", "-f", "agents/foo/note.md")
    git(ovd, "commit", "-q", "-m", "add note", "--no-verify")
    r = cli("check-secrets", ws=ws)
    assert r.returncode == 1
    assert "possible secret (github-token)" in r.stderr


def test_status_without_overlay_exits_3(tmp_path):
    r = cli("status", ws=tmp_path)
    assert r.returncode == 3
    assert "no overlay; run: python3 bin/overlay.py init" in r.stderr


def test_backup_roots_include_an_existing_overlay(tmp_path):
    data, config = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    config.mkdir()
    roots, _ = backup._sources(data, config)
    assert ("overlay", config.parent / "overlay") not in roots
    (config.parent / "overlay").mkdir()
    roots, _ = backup._sources(data, config)
    assert ("overlay", config.parent / "overlay") in roots


def test_backup_and_restore_round_trip_with_an_overlay_repo(tmp_path):
    """The overlay's git objects are read-only; a restore onto the live overlay must not fail,
    and the overlay's own git history is not part of the backup."""
    ws = tmp_path / "install"
    data, config = ws / "data", ws / "config"
    data.mkdir(parents=True)
    write(config / "agents.yaml", "agents: {}\n")
    ovd = init_overlay(ws)
    write(ovd / "config" / "agents.yaml", "agents: {a: 1}\n")
    git(ovd, "add", "config/agents.yaml")
    assert git(ovd, "commit", "-q", "-m", "seam").returncode == 0

    out = backup.backup(data, config, tmp_path / "backups")
    m = backup.verify(out)
    assert any(f["path"] == "overlay/config/agents.yaml" for f in m["files"])
    assert not any("/.git/" in f["path"] for f in m["files"])

    write(ovd / "config" / "agents.yaml", "agents: {changed: true}\n")
    backup.restore(out, data_dir=data, config_dir=config)  # must not raise PermissionError
    assert (ovd / "config" / "agents.yaml").read_text() == "agents: {a: 1}\n"
    assert (ovd / ".git").is_dir()
