#!/usr/bin/env python3
"""
inject-recall.py — UserPromptSubmit re-injection of a recall block (#98).

Today, memory only ever enters a session once: bin/agent-server.py reads a
persona/last-session-summary file at spawn and folds it into
--append-system-prompt. Nothing refreshes it afterward, so a session that
has been running a week answers every question from whatever was true when
it started, and the only way to pull in anything newer is a restart that
also destroys the conversation (defeating the point).

This hook re-injects a recall block before every user message instead,
without requiring a restart. It is fired via config/claude-settings.json's
UserPromptSubmit list alongside log-user-prompt.sh, and reaches the running
session through Claude Code's hookSpecificOutput.additionalContext
mechanism — the same channel PR's household original
(pty-supervisor/hooks/user_prompt_submit.py) uses.

The package ships no Mnemosyne, no knowledge graph, no household-specific
recall store — so the recall source is a documented, swappable interface
rather than a hardcoded lookup:

  KARAKOS_RECALL_SOURCE (env var, default "$WORKSPACE_ROOT/config/recall-source")
    - Path does not exist            -> no-op. Not an error.
    - Path is executable             -> run it with the pending user prompt
                                         text on stdin; its stdout (if any)
                                         becomes the recall block. Non-zero
                                         exit, a timeout, or a crash are all
                                         swallowed as no recall available —
                                         a broken recall source must never
                                         block the user's turn.
    - Path is a plain (non-exec) file -> its contents are read verbatim,
                                         every turn, as a static recall
                                         block (e.g. a hand-maintained facts
                                         file an operator edits directly).

Skip gate: automated traffic (system pokes, heartbeats, task-complete
notifications — anything bin/poke.sh sent, which agent-server.py always
marks is_bot=1) never pays for recall. The is_bot flag itself lives on the
message_queue row and never reaches this hook — all it sees is the final
prompt text over stdin — so agent-server.py stamps a literal sentinel,
AUTOMATED_TRAFFIC_SENTINEL ("[KARAKOS_AUTOMATED]"), onto the front of any
batch where every message is bot-originated. This hook's only job on the
skip side is recognizing that same literal string. Keep the two in sync if
either changes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))

# Must match AUTOMATED_TRAFFIC_SENTINEL in bin/agent-server.py exactly.
AUTOMATED_TRAFFIC_SENTINEL = "[KARAKOS_AUTOMATED]"

DEFAULT_RECALL_SOURCE = WORKSPACE_ROOT / "config" / "recall-source"
RECALL_SOURCE = Path(os.environ.get("KARAKOS_RECALL_SOURCE", str(DEFAULT_RECALL_SOURCE)))

# A recall source that hangs must never hang the user's turn. Overridable
# (KARAKOS_RECALL_TIMEOUT_S) for a slow-but-legitimate recall script, or a
# tight bound in tests.
SUBPROCESS_TIMEOUT_S = float(os.environ.get("KARAKOS_RECALL_TIMEOUT_S", "10"))


def is_automated_traffic(prompt_text: str) -> bool:
    return prompt_text.lstrip().startswith(AUTOMATED_TRAFFIC_SENTINEL)


def load_recall(prompt_text: str) -> str:
    """Resolve RECALL_SOURCE per the interface documented above. Every
    failure mode returns "" (no-op) rather than raising — a missing or
    broken recall source is never allowed to be an error."""
    try:
        if not RECALL_SOURCE.exists():
            return ""

        if os.access(RECALL_SOURCE, os.X_OK):
            try:
                proc = subprocess.run(
                    [str(RECALL_SOURCE)],
                    input=prompt_text,
                    capture_output=True,
                    text=True,
                    timeout=SUBPROCESS_TIMEOUT_S,
                )
            except (subprocess.TimeoutExpired, OSError):
                return ""
            if proc.returncode != 0:
                return ""
            return proc.stdout.strip()

        return RECALL_SOURCE.read_text().strip()
    except OSError:
        return ""


def main() -> int:
    try:
        hook_input = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except json.JSONDecodeError:
        hook_input = {}

    prompt_text = hook_input.get("prompt", "")
    if not prompt_text:
        return 0

    if is_automated_traffic(prompt_text):
        return 0

    recall = load_recall(prompt_text)
    if not recall:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"[ACTIVE RECALL]\n\n{recall}",
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
