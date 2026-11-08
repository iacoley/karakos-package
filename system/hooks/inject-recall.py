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

Recall source, in order:

  1. An operator override REPLACES the graph (never adds to it). It applies
     when KARAKOS_RECALL_SOURCE is set, or $WORKSPACE_ROOT/config/recall-source
     exists:
       - Path does not exist            -> no-op. Not an error.
       - Path is executable             -> run it with the pending user prompt
                                           text on stdin; its stdout (if any)
                                           becomes the recall block. Non-zero
                                           exit, a timeout, or a crash are all
                                           swallowed as no recall available.
       - Path is a plain (non-exec) file -> its contents are read verbatim,
                                           every turn, as a static block.
  2. Otherwise the knowledge graph (lib/graph, data/memory/graph.db) is
     queried with the prompt text: KARAKOS_RECALL_LIMIT results (default 6)
     rendered as "- [kind] subject: text" lines, capped at
     KARAKOS_RECALL_MAX_CHARS (default 2400). An uninitialised or broken
     graph, an error, or a timeout (KARAKOS_RECALL_TIMEOUT_S, default 10,
     enforced with signal.alarm) yields no block, never an error.

Hook cost: this is a fresh process per prompt and loading the embedding model
costs ~6 s and ~230 MB, so the graph is queried in fast mode (no model load;
keyword, name and importance signals only). KARAKOS_RECALL_HOOK_MODE=full
tries the model within a 6 s budget and falls back to fast. The warm `memory`
tool stays hybrid.

This hook is the only per-prompt recall path. (bin/agent-server.py separately
loads top graph facts at spawn under a different header, for automated turns
that skip this hook.)

Voice reminder (opt-in by file): when agents/$KARAKOS_AGENT/voice_reminder.md
exists, its text is appended to the injected context on every human turn,
followed by KARAKOS_VOICE_EXEMPLARS (default 2) entries picked at random from
agents/$KARAKOS_AGENT/voice_exemplars.md (entries separated by a line holding
only "---"). A persona file in the system prompt fades over a long session;
text next to the turn does not. It is independent of recall: an empty recall
block does not suppress it. Absent files mean no reminder.

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
import math
import os
import random
import re
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../lib")))
from paths import workspace_root  # noqa: E402
import signal
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(workspace_root())

# Must match AUTOMATED_TRAFFIC_SENTINEL in bin/agent-server.py exactly.
AUTOMATED_TRAFFIC_SENTINEL = "[KARAKOS_AUTOMATED]"

DEFAULT_RECALL_SOURCE = WORKSPACE_ROOT / "config" / "recall-source"
_ENV_SOURCE = os.environ.get("KARAKOS_RECALL_SOURCE")
RECALL_SOURCE = Path(_ENV_SOURCE or str(DEFAULT_RECALL_SOURCE))
# An override replaces the graph source; absent both, the graph is the source.
HAS_OVERRIDE = bool(_ENV_SOURCE) or DEFAULT_RECALL_SOURCE.exists()


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


RECALL_LIMIT = _int_env("KARAKOS_RECALL_LIMIT", 6)
RECALL_MAX_CHARS = _int_env("KARAKOS_RECALL_MAX_CHARS", 2400)
HOOK_MODE = os.environ.get("KARAKOS_RECALL_HOOK_MODE", "fast").strip().lower()
FULL_MODE_EMBED_BUDGET_S = "6"

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


class _Timeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def _render(results, max_chars: int) -> str:
    lines = []
    total = 0
    for r in results:
        text = " ".join(str(r.get("content") or "").split())
        subject = r.get("subject") or r.get("subkind") or r.get("domain") or "note"
        line = f"- [{r.get('kind')}] {subject}: {text}"
        if total + len(line) + 1 > max_chars:
            room = max_chars - total - 1
            if room > 40 and not lines:
                lines.append(line[:room].rstrip())
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _graph_query(prompt_text: str) -> str:
    for root in (Path(__file__).resolve().parents[2], WORKSPACE_ROOT):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    from lib.graph.recall import recall
    from lib.graph.store import open_graph
    store = open_graph(WORKSPACE_ROOT / "data", create=False)
    if HOOK_MODE == "full":
        os.environ["KARAKOS_RECALL_EMBED_BUDGET"] = FULL_MODE_EMBED_BUDGET_S
        mode = "auto"
    else:
        mode = "keyword"
    res = recall(store, prompt_text, limit=RECALL_LIMIT, mode=mode)
    return _render(_with_subjects(store, res.get("results", [])), RECALL_MAX_CHARS)


def _with_subjects(store, results):
    """Attach the owning entity's name as `subject` (one read, no model)."""
    ids = {r["entity_id"] for r in results if r.get("entity_id") is not None}
    names = {}
    if ids:
        with store.read() as conn:
            for row in conn.execute(
                    f"SELECT id, name FROM entities WHERE id IN ({','.join('?' * len(ids))})",
                    list(ids)):
                names[row["id"]] = row["name"]
    return [{**r, "subject": names.get(r.get("entity_id"))} for r in results]


def load_graph_recall(prompt_text: str) -> str:
    """Graph recall under a hard wall-clock bound. Any failure -> ""."""
    try:
        old = signal.signal(signal.SIGALRM, _on_alarm)
    except (ValueError, AttributeError):
        old = None
    try:
        signal.alarm(max(1, math.ceil(SUBPROCESS_TIMEOUT_S)))
        return _graph_query(prompt_text)
    except BaseException as e:  # noqa: BLE001 - never fail the user's turn
        if isinstance(e, KeyboardInterrupt):
            raise
        return ""
    finally:
        signal.alarm(0)
        if old is not None:
            signal.signal(signal.SIGALRM, old)


def _int_opt(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def load_voice_reminder() -> str:
    """The agent's voice reminder plus a few random exemplars. Any failure,
    a missing agent id or a missing file returns "" (never an error)."""
    agent = os.environ.get("KARAKOS_AGENT", "").strip()
    if not agent or "/" in agent or agent.startswith("."):
        return ""
    try:
        base = WORKSPACE_ROOT / "agents" / agent
        reminder = (base / "voice_reminder.md").read_text().strip()
        if not reminder:
            return ""
        parts = [reminder]
        count = _int_opt("KARAKOS_VOICE_EXEMPLARS", 2)
        try:
            raw = (base / "voice_exemplars.md").read_text()
        except OSError:
            raw = ""
        entries = [e.strip() for e in re.split(r"(?m)^---[ \t]*$", raw) if e.strip()]
        if entries and count:
            picked = random.sample(entries, min(count, len(entries)))
            parts.append("Exemplars of the register (take the rhythm, not the words):\n\n"
                         + "\n\n".join(picked))
        return "\n\n".join(parts)
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

    recall = load_recall(prompt_text) if HAS_OVERRIDE else load_graph_recall(prompt_text)
    voice = load_voice_reminder()
    blocks = []
    if recall:
        blocks.append(f"[ACTIVE RECALL]\n\n{recall}")
    if voice:
        # Last, so it sits closest to generation.
        blocks.append(f"[VOICE]\n\n{voice}")
    if not blocks:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(blocks),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
