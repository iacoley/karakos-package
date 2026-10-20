#!/usr/bin/env python3
"""
protected-paths-edit.py — PreToolUse edit-time gate for protected paths.

Protection used to be commit-time only (system/check-protected-paths.py
--staged), so an agent could rewrite bin/relay.py on the running volume and
nothing stopped it until a commit. This hook applies the same rules, from the
same config/protected-paths.json and the same matching code, at the moment the
edit happens.

  * Edit / Write / MultiEdit (tool_input.file_path) and NotebookEdit
    (tool_input.notebook_path): the target path is checked.
  * Bash: every non-option argument of tee, cp, mv, rm, sed, install, ln,
    truncate, dd (also the value after of=), chmod, chown and patch is checked
    as a path. Relative paths resolve against the payload `cwd`, else the root.

Order: unprotected override (allow, no log) -> tier 1 (deny) -> tier 2 (allow,
logged) -> allow. Tier 1 prints a PreToolUse deny decision; tier 2 prints
nothing. Every tier 1 deny and tier 2 allow is appended to
$ROOT/logs/protected-edits.jsonl with keys ts, agent, tool, path, tier, decision.

Agents only: when KARAKOS_AGENT is unset or empty (operator / CLI sessions) the
hook does nothing. Paths are resolved with os.path.realpath, so a symlink
inside the root that points at a protected file is still caught; a path outside
the root is not protected. ROOT is $WORKSPACE_ROOT, else the repo root two
levels above this file.

Fail-open: unparseable stdin, a missing field, or a missing/unreadable
config/protected-paths.json (one line to stderr) allow the call.

Known gap: shell redirections (`>`, `>>`) are dropped by lib/bashcmd.py, so
`echo x > bin/relay.py` is not detected. Nor are interpreters that write files
themselves (python -c, perl -i, awk, ...). Edit/Write/MultiEdit/NotebookEdit
and the listed commands are covered; this is a guard rail, not a sandbox.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

_FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
_BASH_WRITERS = {"tee", "cp", "mv", "rm", "sed", "install", "ln", "truncate",
                 "dd", "chmod", "chown", "patch"}


def _load_rules():
    """The matching code of system/check-protected-paths.py (hyphenated name)."""
    path = os.path.join(os.path.dirname(HERE), "check-protected-paths.py")
    spec = importlib.util.spec_from_file_location("check_protected_paths", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _classify(path, root_real, rules, cfg):
    """Return (relative path, tier) for a protected path, else (relative path, 0).
    Returns (None, 0) when the path is outside the root."""
    try:
        real = os.path.realpath(path)
        rel = os.path.relpath(real, root_real).replace(os.sep, "/")
    except Exception:
        return None, 0
    if rel == "." or rel == ".." or rel.startswith("../"):
        return None, 0
    if rules.is_override(rel, cfg.get("unprotected_overrides", [])):
        return rel, 0
    if rules.check_tier1(rel, cfg.get("tier1_protected", [])):
        return rel, 1
    if rules.check_tier2(rel, cfg.get("tier2_review_required", [])):
        return rel, 2
    return rel, 0


def _bash_paths(payload, root):
    import bashcmd
    cmd = (payload.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    cwd = payload.get("cwd")
    base = cwd if isinstance(cwd, str) and cwd else root
    home = os.environ.get("HOME")
    out = []
    for argv in bashcmd.commands(cmd):
        name = os.path.basename(str(argv[0]))
        if name not in _BASH_WRITERS:
            continue
        for word in argv[1:]:
            text = str(word)
            if name == "dd" and text.startswith("of="):
                word, text = bashcmd.Word(word.raw[3:], word.glob), text[3:]
            elif text.startswith("-"):
                continue
            expanded = bashcmd.expand_word(word, os.environ, home)
            if expanded is None:
                if not getattr(word, "glob", False):
                    continue
                expanded = text
                matches = glob.glob(os.path.join(base, expanded))
                out.extend(matches)
                continue
            if expanded:
                out.append(os.path.join(base, expanded))  # absolute paths win
    return out


def _log(root, agent, tool, rel, tier, decision):
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "agent": agent, "tool": tool, "path": rel, "tier": tier,
               "decision": decision}
        with open(os.path.join(logs, "protected-edits.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main() -> None:
    agent = os.environ.get("KARAKOS_AGENT")
    if not agent:
        return
    try:
        payload = json.load(sys.stdin)
        tool = payload.get("tool_name")
        root = os.environ.get("WORKSPACE_ROOT") or os.path.dirname(os.path.dirname(HERE))
        if tool in _FILE_TOOLS:
            fp = (payload.get("tool_input") or {}).get(_FILE_TOOLS[tool])
            if not fp or not isinstance(fp, str):
                return
            targets = [fp]
        elif tool == "Bash":
            targets = _bash_paths(payload, root)
        else:
            return
        if not targets:
            return
    except Exception:
        return

    try:
        root_real = os.path.realpath(root)
        with open(os.path.join(root_real, "config", "protected-paths.json")) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("not a JSON object")
        rules = _load_rules()
    except Exception as exc:
        print(f"protected-paths-edit: rules unavailable ({exc}); allowing", file=sys.stderr)
        return

    tier2 = []
    for target in targets:
        try:
            if not os.path.isabs(target):
                target = os.path.join(root_real, target)
            rel, tier = _classify(target, root_real, rules, cfg)
        except Exception:
            continue
        if tier == 1:
            _log(root_real, agent, tool, rel, 1, "deny")
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"Protected path (tier 1): {rel}. Do not edit it; report to the owner.",
            }}))
            return
        if tier == 2:
            tier2.append(rel)
    for rel in tier2:
        _log(root_real, agent, tool, rel, 2, "allow")


if __name__ == "__main__":
    main()
