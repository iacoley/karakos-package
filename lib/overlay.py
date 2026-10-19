"""Private overlay repo for per-install data (#243, "MÍRDAIN G12: a private overlay repo for per-install data"). Pure functions, no network.

The overlay (`<workspace>/overlay/`, its own git repo, gitignored by the package) holds
named seams: install config, personas and skills. It is not a mirror of the install.
Precedence: package defaults, then overlay. `apply` copies seam files overlay -> install;
`capture` copies them install -> overlay. Neither ever writes a path the package tracks
in git, nor a skill directory the package ships. See docs/OVERLAY.md.
"""
from __future__ import annotations

import fnmatch
import functools
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
OVERLAY_DIRNAME = "overlay"
REMOTE_KEY = "KARAKOS_OVERLAY_REPO"

FILE_SEAMS = ("config/agents.yaml", "config/channels.json")
DIR_SEAMS = ("memory-exports",)
AGENTS = "agents"
SKILLS = "skills"
EXCLUDED_AGENT_DIRS = {"templates"}  # agents/CORE.md and agents/HOUSE_STYLE.md are files, never walked
SECRETS_MANIFEST = "secrets.manifest"  # overlay root only; never a seam
SKIP_DIR_NAMES = {"__pycache__", "inbox", "journal", ".git"}
SKIP_NAME_GLOBS = ("*.generated.md",)

ACTIONS = ("copy", "skip-tracked", "unchanged")


@functools.lru_cache(maxsize=None)
def _guard():
    """system/check-secrets.py, loaded by path: its forbidden-path list is the one source of truth."""
    spec = importlib.util.spec_from_file_location("check_secrets", PKG_ROOT / "system" / "check-secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def overlay_dir(workspace) -> Path:
    return Path(workspace) / OVERLAY_DIRNAME


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def configured_remote(workspace, environ=None) -> str:
    """KARAKOS_OVERLAY_REPO from the environment, else a `KARAKOS_OVERLAY_REPO=` line in
    config/.env. The file is parsed, never sourced. Empty string when neither is set."""
    env = os.environ if environ is None else environ
    if env.get(REMOTE_KEY):
        return env[REMOTE_KEY].strip()
    value = ""
    try:
        lines = (Path(workspace) / "config" / ".env").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        if key.strip().removeprefix("export ").strip() == REMOTE_KEY:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            value = val
    return value


def _forbidden(rel: str) -> bool:
    return _guard().forbidden_path(rel) is not None


def _walk(root: Path, base: str):
    top = root / base
    if not top.is_dir():
        return
    for p in sorted(top.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")
        if any(x in SKIP_DIR_NAMES for x in parts[:-1]):
            continue
        if any(fnmatch.fnmatch(parts[-1], g) for g in SKIP_NAME_GLOBS):
            continue
        if _forbidden(rel):
            continue
        yield rel


def list_seam_files(root):
    """Repo-relative POSIX paths of seam files under `root` (overlay or install)."""
    root = Path(root)
    for rel in FILE_SEAMS:
        if (root / rel).is_file() and not _forbidden(rel):
            yield rel
    for base in DIR_SEAMS:
        yield from _walk(root, base)
    for base, skip in ((AGENTS, EXCLUDED_AGENT_DIRS), (SKILLS, set())):
        top = root / base
        if not top.is_dir():
            continue
        for d in sorted(top.iterdir()):
            if d.is_symlink() or not d.is_dir() or d.name.startswith(".") or d.name in skip:
                continue
            yield from _walk(root, f"{base}/{d.name}")


def tracked_by_package(workspace) -> set[str]:
    """Paths `git ls-files` lists for the package checkout. Empty set when there is no
    checkout. If a .git exists but git cannot read it, raise: an unreadable checkout must
    not look like an untracked one, or apply could overwrite tracked files."""
    ws = Path(workspace)
    if not (ws / ".git").exists():
        return set()
    top = _git(ws, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(f"cannot read package checkout at {ws}: {top.stderr.strip()}")
    if Path(top.stdout.strip()).resolve() != ws.resolve():
        return set()
    ls = _git(ws, "ls-files", "-z")
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-files failed at {ws}: {ls.stderr.strip()}")
    return {p for p in ls.stdout.split("\0") if p}


def _shipped_skills(tracked: set[str]) -> set[str]:
    return {p.split("/")[1] for p in tracked if p.startswith(SKILLS + "/") and len(p.split("/")) >= 3}


def _excluded(rel: str, tracked: set[str], shipped: set[str]) -> bool:
    if rel in tracked:
        return True
    parts = rel.split("/")
    return parts[0] == SKILLS and len(parts) >= 3 and parts[1] in shipped


def _same(a: Path, b: Path) -> bool:
    return b.is_file() and a.read_bytes() == b.read_bytes()


def _plan(src_root: Path, dst_root: Path, tracked: set[str]) -> list[tuple[str, str]]:
    shipped = _shipped_skills(tracked)
    plan = []
    for rel in sorted(set(list_seam_files(src_root))):
        if _excluded(rel, tracked, shipped):
            action = "skip-tracked"
        elif _same(src_root / rel, dst_root / rel):
            action = "unchanged"
        else:
            action = "copy"
        plan.append((action, rel))
    return plan


def plan_apply(workspace) -> list[tuple[str, str]]:
    ov = overlay_dir(workspace)
    if not ov.is_dir():
        return []
    return _plan(ov, Path(workspace), tracked_by_package(workspace))


def plan_capture(workspace) -> list[tuple[str, str]]:
    return _plan(Path(workspace), overlay_dir(workspace), tracked_by_package(workspace))


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    try:
        shutil.copymode(src, dst)  # skill scripts keep their exec bit
    except OSError:
        pass


def apply(workspace) -> list[tuple[str, str]]:
    plan = plan_apply(workspace)
    ov, ws = overlay_dir(workspace), Path(workspace)
    for action, rel in plan:
        if action == "copy":
            _copy(ov / rel, ws / rel)
    return plan


def capture(workspace) -> list[tuple[str, str]]:
    plan = plan_capture(workspace)
    ov, ws = overlay_dir(workspace), Path(workspace)
    for action, rel in plan:
        if action == "copy":
            _copy(ws / rel, ov / rel)
    return plan


def hook_text(guard_path: Path = PKG_ROOT / "system" / "check-secrets.py") -> str:
    return ("#!/usr/bin/env bash\n"
            'WORKSPACE_ROOT="$(git rev-parse --show-toplevel)" '
            f'exec python3 "{guard_path}" --staged\n')


def install_hook(overlay_root) -> Path:
    hook = Path(overlay_root) / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(hook_text())
    hook.chmod(0o755)
    return hook


def scan_tracked(overlay_root) -> list[str]:
    """check-secrets rules over every tracked file of the overlay. Returns problem lines."""
    guard = _guard()
    ov = Path(overlay_root)
    allow = guard.load_allow(ov)
    problems = []
    ls = _git(ov, "ls-files", "-z")
    if ls.returncode != 0:
        return [f"  git ls-files failed: {ls.stderr.strip()}"]
    for f in (p for p in ls.stdout.split("\0") if p):
        why = guard.forbidden_path(f)
        if why:
            problems.append(f"  {f}: {why} must not be committed")
            continue
        if f.startswith(guard.SAMPLES_DIR):
            continue
        try:
            text = (ov / f).read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            name = guard.scan_line(line, allow)
            if name:
                problems.append(f"  {f}: possible secret ({name})")
                break
    return problems


def git_commit(repo, message: str) -> subprocess.CompletedProcess:
    """Commit with a neutral identity only when the repo has none configured."""
    extra = []
    if not _git(repo, "config", "user.email").stdout.strip():
        extra = ["-c", "user.name=Karakos overlay", "-c", "user.email=overlay@karakos.invalid"]
    return _git(repo, *extra, "commit", "-m", message)
