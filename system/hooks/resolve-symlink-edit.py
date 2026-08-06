#!/usr/bin/env python3
"""
resolve-symlink-edit.py — PreToolUse self-heal for the Edit/Write symlink
block.

Claude Code refuses to Edit/Write through a symlink:
    "Refusing to write through symlink: <path>. Resolve the symlink and pass
     the real target path explicitly."
That costs a wasted turn every time — the model has to notice the failure,
run realpath, and re-issue. It recurs structurally in installs with
symlinked config, shared checkouts mounted via symlink, or any tree where a
path component is a link rather than a real directory.

This hook fixes it transparently: on any Edit/Write whose file_path resolves
through a symlink, it rewrites file_path to the fully-resolved real path via
PreToolUse `updatedInput`, so the tool runs on the real target and the model
never sees the rejection. Targets harness behaviour rather than any specific
household's layout, so it applies to any install with a symlink anywhere in
its tree.

Read is rewritten too, and that is load-bearing rather than incidental. The
harness tracks read-state per PATH STRING, and the check runs against the
post-hook (rewritten) path. Rewriting only the write side desyncs the two:
Read("<symlink>") records the symlink path while the Edit is checked against
the realpath, which surfaces as "File content has changed since it was last
read" — trading one spurious failure for another. Rewriting both sides makes
Read and Edit agree on the resolved path, so read-then-edit through a
symlink just works.

Fail-safe by construction: any parse error, missing path, or non-symlink
path emits nothing, so the tool proceeds exactly as it would have
unmodified. This hook only ever rewrites a path to its own realpath — it
never blocks, denies, or changes file content.

Scope note: it does NOT (and cannot) touch the sibling "File has not been
read yet" guardrail — that is internal harness read-state with no hook
bypass.

Wired via config/claude-settings.json (PreToolUse, matcher
"Edit|Write|MultiEdit|Read") and the --settings flag on the claude spawn
line in bin/agent-server.py (#94).
"""
from __future__ import annotations

import json
import os
import sys

# Tools that carry a `file_path`. Read is included so its recorded read-state
# keys on the same resolved path the write tools are checked against.
_TARGET_TOOLS = {"Edit", "Write", "MultiEdit", "Read"}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # unparseable — stay out of the way

    tool = payload.get("tool_name")
    if tool not in _TARGET_TOOLS:
        return

    tool_input = payload.get("tool_input") or {}
    fp = tool_input.get("file_path")
    if not fp or not isinstance(fp, str):
        return

    try:
        # realpath resolves symlinks in ANY path component (leaf file symlink,
        # or a parent directory symlink).
        real = os.path.realpath(fp)
    except Exception:
        return

    # Only act when resolution actually changes the path AND a symlink is
    # genuinely involved somewhere in it. abspath (no symlink resolution)
    # differing from realpath is the precise signal.
    if real == os.path.abspath(fp):
        return

    # Rewrite file_path to the real target; preserve every other field.
    new_input = dict(tool_input)
    new_input["file_path"] = real

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": new_input,
        }
    }))


if __name__ == "__main__":
    main()
