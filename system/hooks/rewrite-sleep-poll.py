#!/usr/bin/env python3
"""
rewrite-sleep-poll.py — PreToolUse rewrite for the sandbox-blocked
foreground sleep-poll.

Foreground `sleep` is blocked in the sandbox. Agents reach for it anyway —
`sleep 300; gh pr checks 674`, `sleep 60 && tail -20 /tmp/harvest.log` — and
the harness rejects the whole command with an error telling the model to
wait some other way. The correction can sit in the system prompt already
and still recur, because it's a reflex, not a knowledge gap — this
household logged 17 occurrences of exactly that in 6 days (7 in a single
24-hour window) WITH the correction already present in the prompt. The cost
is a wasted turn every time: the model has to notice the rejection and
re-issue.

So rewrite it instead of letting it fail. `sleep N <sep> <rest>` becomes
`$WORKSPACE_ROOT/bin/wait-for.sh --sleep N <sep> <rest>` (issue #93), which
is the permitted equivalent and preserves the original semantics exactly —
wait N seconds, then run the rest, with `&&` / `;` chaining behaviour
unchanged.

Deliberately NOT clever: it does not try to infer a poll condition from the
trailing command and turn it into `wait-for.sh "<condition>"`. That would be
the better command in many cases, but it changes semantics — a condition
returns as soon as it is true, a delay always waits the full N — and
guessing wrong silently produces different behaviour than the author asked
for. The faithful rewrite is the safe one; picking a real condition stays
the author's job, and wait-for.sh's own usage text says so.

Only fires when the command STARTS with `sleep <number>` and something
follows. A bare `sleep N` with nothing after it is left alone: there is no
wasted work to save, and the rejection is the correct outcome.

Fail-safe by construction: any parse error, non-Bash tool, or non-matching
command emits nothing, so the tool proceeds exactly as it would have.

Wired via config/claude-settings.json (PreToolUse, matcher "Bash") and the
--settings flag on the claude spawn line in bin/agent-server.py (#94).
"""
from __future__ import annotations

import json
import re
import sys

WAIT_FOR = "$WORKSPACE_ROOT/bin/wait-for.sh"

# `sleep 300;` / `sleep 5 && ` / `sleep 0.5 ;` at the very start, with a
# non-empty remainder. Anchored so a sleep buried inside a loop body or a
# quoted string is never touched.
_PATTERN = re.compile(r"^\s*sleep\s+(\d+(?:\.\d+)?)\s*(&&|;)\s*(\S.*)$", re.DOTALL)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    if payload.get("tool_name") != "Bash":
        return

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return

    m = _PATTERN.match(command)
    if not m:
        return

    delay, sep, rest = m.group(1), m.group(2), m.group(3)

    # Integer seconds only — wait-for.sh --sleep compares whole seconds.
    seconds = str(int(float(delay))) if float(delay) >= 1 else "1"

    rewritten = f"{WAIT_FOR} --sleep {seconds} {sep} {rest}"

    updated = dict(tool_input)
    updated["command"] = rewritten

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
            },
            "systemMessage": (
                f"Rewrote a blocked foreground `sleep {delay}` into "
                f"`wait-for.sh --sleep {seconds}`. If you are waiting for a "
                f"condition rather than a fixed delay, "
                f'`wait-for.sh "<condition>"` returns as soon as it is true.'
            ),
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
