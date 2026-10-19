#!/usr/bin/env python3
"""overlay -- the private overlay repo for per-install data (#243, "MÍRDAIN G12: a private overlay repo for per-install data"). See docs/OVERLAY.md.

  python3 bin/overlay.py init [--remote URL] [--workspace DIR]
  python3 bin/overlay.py status | apply [--quiet] | capture | pull | push | check-secrets

The overlay is <workspace>/overlay/, a separate git repo. The workspace defaults to
$WORKSPACE_ROOT, else this checkout. Exit 3 when there is no overlay.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))  # import as lib.overlay: bin/ itself must not shadow this module

from lib import overlay as ov  # noqa: E402

NO_OVERLAY = "no overlay; run: python3 bin/overlay.py init"


def workspace_of(args) -> Path:
    return Path(args.workspace or os.environ.get("WORKSPACE_ROOT") or PKG).resolve()


def _git(repo, *args, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=capture, text=True, check=False)


def _has_overlay(ovd: Path) -> bool:
    if (ovd / ".git").is_dir():
        return True
    print(NO_OVERLAY, file=sys.stderr)
    return False


def _write_scaffold(ovd: Path) -> None:
    files = {
        ".gitignore": ".env*\n*.pem\n*.key\nsecrets/\n",
        "README.md": (
            "# Karakos overlay\n\n"
            "Per-install data for one Karakos install, kept out of the package repo.\n"
            "`config/agents.yaml`, `config/channels.json`, `agents/<name>/` (not `templates/`),\n"
            "`skills/<name>/` (not package-shipped skills), `memory-exports/`.\n"
            "Secrets never go here: `secrets.manifest` lists env var NAMES only.\n"
            "Apply with `python3 bin/overlay.py apply` in the install; see docs/OVERLAY.md.\n"
        ),
        "secrets.manifest": (
            "# One secret NAME per line (an env var name). Values never live in this repo.\n"
        ),
    }
    for name, text in files.items():
        if not (ovd / name).exists():
            (ovd / name).write_text(text)


def cmd_init(args) -> int:
    ws = workspace_of(args)
    ovd = ov.overlay_dir(ws)
    if (ovd / ".git").is_dir():
        print("overlay already initialised")
        return 0
    remote = args.remote or ov.configured_remote(ws)
    if not ovd.exists() and remote:
        r = subprocess.run(["git", "clone", remote, str(ovd)], check=False)
        if r.returncode:
            return r.returncode
        ov.install_hook(ovd)
        print(f"cloned {remote} into {ovd}")
        return 0
    if ovd.exists() and any(ovd.iterdir()):
        print(f"{ovd} exists and is not a git repo; move it aside", file=sys.stderr)
        return 1
    ovd.mkdir(parents=True, exist_ok=True)
    _git(ovd, "init", "-b", "main")
    _write_scaffold(ovd)
    ov.install_hook(ovd)
    if remote:
        _git(ovd, "remote", "add", "origin", remote)
    _git(ovd, "add", ".gitignore", "README.md", "secrets.manifest")
    c = ov.git_commit(ovd, "Initial overlay")
    if c.returncode:
        print(c.stdout + c.stderr, file=sys.stderr)
        return c.returncode
    print(f"overlay initialised at {ovd}" + (f", origin {remote}" if remote else ""))
    return 0


def cmd_status(args) -> int:
    ws = workspace_of(args)
    ovd = ov.overlay_dir(ws)
    if not _has_overlay(ovd):
        return 3
    remote = _git(ovd, "remote", "get-url", "origin").stdout.strip() or "(none)"
    branch = _git(ovd, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "(detached)"
    print(f"overlay: {ovd}")
    print(f"remote: {remote}")
    print(f"branch: {branch}")
    print(f"seam files in overlay: {len(list(ov.list_seam_files(ovd)))}")
    groups: dict[str, list[str]] = {a: [] for a in ov.ACTIONS}
    for action, rel in ov.plan_apply(ws):
        groups[action].append(rel)
    for action in ov.ACTIONS:
        print(f"{action}: {len(groups[action])}")
        for rel in groups[action]:
            print(f"  {rel}")
    return 0


def cmd_apply(args) -> int:
    ws = workspace_of(args)
    if not _has_overlay(ov.overlay_dir(ws)):
        return 3
    plan = ov.apply(ws)
    if not args.quiet:
        for action, rel in plan:
            if action in ("copy", "skip-tracked"):
                print(f"{action} {rel}")
    return 0


def cmd_capture(args) -> int:
    ws = workspace_of(args)
    ovd = ov.overlay_dir(ws)
    if not _has_overlay(ovd):
        return 3
    for action, rel in ov.capture(ws):
        if action in ("copy", "skip-tracked"):
            print(f"{action} {rel}")
    _git(ovd, "add", "-A")
    if _git(ovd, "diff", "--cached", "--quiet").returncode == 0:
        print("nothing to capture")
        return 0
    c = ov.git_commit(ovd, "Capture install state")
    if c.returncode:
        print(c.stdout + c.stderr, file=sys.stderr)
    return c.returncode


def cmd_pull(args) -> int:
    ws = workspace_of(args)
    ovd = ov.overlay_dir(ws)
    if not _has_overlay(ovd):
        return 3
    r = _git(ovd, "pull", "--ff-only", capture=False)
    if r.returncode:
        return r.returncode
    for action, rel in ov.apply(ws):
        if action in ("copy", "skip-tracked"):
            print(f"{action} {rel}")
    return 0


def cmd_push(args) -> int:
    ovd = ov.overlay_dir(workspace_of(args))
    if not _has_overlay(ovd):
        return 3
    return _git(ovd, "push", "-u", "origin", "HEAD", capture=False).returncode


def cmd_check_secrets(args) -> int:
    ovd = ov.overlay_dir(workspace_of(args))
    if not _has_overlay(ovd):
        return 3
    problems = ov.scan_tracked(ovd)
    if problems:
        print("check-secrets: overlay blocked", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("check-secrets: overlay clean")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", metavar="DIR", help="install root (default: $WORKSPACE_ROOT or this checkout)")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", parents=[common], help="create overlay/ (or clone the configured remote)")
    s.add_argument("--remote", metavar="URL", help="overlay git remote (default: KARAKOS_OVERLAY_REPO)")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("status", parents=[common], help="show overlay state and the apply plan").set_defaults(fn=cmd_status)
    s = sub.add_parser("apply", parents=[common], help="copy overlay seams into the install")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_apply)
    sub.add_parser("capture", parents=[common], help="copy install seams into overlay and commit").set_defaults(fn=cmd_capture)
    sub.add_parser("pull", parents=[common], help="git pull --ff-only the overlay, then apply").set_defaults(fn=cmd_pull)
    sub.add_parser("push", parents=[common], help="git push the overlay").set_defaults(fn=cmd_push)
    sub.add_parser("check-secrets", parents=[common], help="secret guard over all overlay files").set_defaults(fn=cmd_check_secrets)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
