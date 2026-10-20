#!/usr/bin/env python3
"""
hooks-sync.py — regenerate the `hooks` section of config/claude-settings.json
from config/hooks.json.

Managed entries are those whose command path is under `system/hooks/`; they
are rebuilt on every run. Hooks the user added (any other command) and the
`permissions` / `env` sections are preserved untouched. Idempotent: a second
run changes nothing and does not rewrite the file. config/hooks.json is
created with defaults when absent.

Run from bin/entrypoint.sh and setup.sh. Usage: hooks-sync.py [workspace-root]
(default $WORKSPACE_ROOT, else the repo this script lives in).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULTS = {"heavy_build_block": False, "bare_ssh_hosts": [], "ssh_wrapper": ""}
MANAGED_MARKER = "system/hooks/"
HOOK_CMD = "$WORKSPACE_ROOT/system/hooks/{}"


def _cmd(name: str) -> dict:
    return {"type": "command", "command": HOOK_CMD.format(name)}


def desired(cfg: dict) -> dict[str, list[dict]]:
    bash = [_cmd("bash-safety-rails.py"), _cmd("block-bare-ssh.py")]
    if cfg.get("heavy_build_block") is True:
        bash.append(_cmd("block-heavy-build.py"))
    return {
        "UserPromptSubmit": [
            {"hooks": [_cmd("log-user-prompt.sh"), _cmd("inject-recall.py")]},
        ],
        "PreToolUse": [
            {"matcher": "Edit|Write|MultiEdit|Read", "hooks": [_cmd("resolve-symlink-edit.py")]},
            # One group for the file tools and Bash: a hook script is wired exactly once.
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [_cmd("protected-paths-edit.py")]},
            {"matcher": "Bash", "hooks": bash},
        ],
        "Stop": [
            {"hooks": [_cmd("stop-deferred-work.py")]},
        ],
    }


def _is_managed(hook: dict) -> bool:
    return isinstance(hook, dict) and MANAGED_MARKER in str(hook.get("command", ""))


def _strip_managed(groups) -> list:
    kept = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            kept.append(g)
            continue
        rest = [h for h in g["hooks"] if not _is_managed(h)]
        if rest:
            kept.append({**g, "hooks": rest})
    return kept


def load_hooks_config(root: Path) -> dict:
    path = root / "config" / "hooks.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
        return dict(DEFAULTS)
    try:
        cfg = json.loads(path.read_text())
        return cfg if isinstance(cfg, dict) else dict(DEFAULTS)
    except Exception:
        return dict(DEFAULTS)


def sync(root: Path) -> bool:
    """Rewrite the hooks section. Returns True if the settings file changed."""
    root = Path(root)
    cfg = load_hooks_config(root)
    settings_path = root / "config" / "claude-settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        settings = {"permissions": {"allow": [], "deny": []}, "env": {}}
    if not isinstance(settings, dict):
        raise SystemExit(f"hooks-sync: {settings_path} is not a JSON object; not touching it")
    old_hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}

    new_hooks: dict = {}
    want = desired(cfg)
    for event in list(want) + [e for e in old_hooks if e not in want]:
        groups = want.get(event, []) + _strip_managed(old_hooks.get(event, []))
        if groups:
            new_hooks[event] = groups
    settings["hooks"] = new_hooks

    text = json.dumps(settings, indent=2) + "\n"
    if settings_path.exists() and settings_path.read_text() == text:
        return False
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(text)
    return True


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else
                os.environ.get("WORKSPACE_ROOT") or Path(__file__).resolve().parent.parent)
    changed = sync(root)
    print("hooks-sync: updated" if changed else "hooks-sync: unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
