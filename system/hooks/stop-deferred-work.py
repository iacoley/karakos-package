#!/usr/bin/env python3
"""
stop-deferred-work.py — Stop hook: catch a promised-but-never-done turn.

An agent can end its turn with "I'll check that shortly" or "On it." and
nothing re-invokes it — no background self, no next beat. The user is left
holding a commitment with nothing behind it. This hook runs a regex catalog
against the tail of the just-finished assistant reply; when it matches a
deferral phrase and the session hasn't already hit the extension cap, it
emits {"decision": "block", "reason": "..."} — the Stop-hook contract Claude
Code uses to force the turn to continue instead of ending — with a
continuation prompt telling the model to do the work now instead of
re-promising it.

The extension cap is load-bearing, not decorative: without it, a model that
re-emits the same deferral phrase in its continuation (because the prior
block prompt told it to "continue with that work" and it interprets that as
"acknowledge and defer again") loops forever, burning turns without
producing anything. Capped at MAX_EXTENSIONS (2) per session; once hit, the
turn is allowed to end normally even if the catalog matches again.

The counter resets whenever a new user message appears since the last
reset — otherwise a session that legitimately used up its 2 extensions
earlier in a long conversation would be permanently blocked from ever
extending again, for an unrelated later reply.

Claude Code's own `stop_hook_active` flag (true once a Stop hook has
already forced one continuation in the current stop cycle) is honoured as a
second, independent backstop: even if the on-disk counter were somehow
reset or lost, this hook still refuses to force yet another continuation
once the harness reports one already happened.

Wired via config/claude-settings.json (Stop) and the --settings flag on the
claude spawn line in bin/agent-server.py (#94).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
COUNTER_FILE = WORKSPACE_ROOT / "data" / "stop-hook-extensions.json"
MAX_EXTENSIONS = 2
TAIL_CHARS = 500

# An "immediate" qualifier: now, right away, immediately, asap, shortly, or
# "in a {bit,moment,sec,second,minute}".
_IMMEDIATE = (
    r"(?:now|right away|right now|immediately|asap|shortly|"
    r"in a (?:bit|moment|sec|second|minute))"
)

# Objects the commitment can target: a bare pronoun, or a short determiner-led
# noun phrase ("the build", "this deploy"). Noun phrases require an explicit
# immediate qualifier so a trailing prepositional/temporal phrase ("at 3pm")
# doesn't get absorbed into the noun and produce a false positive.
_PRONOUN = r"(?:that|it|this|them)"
_NOUN_PHRASE = r"(?:(?:the|this|that|my|our|a|an)\s+\w+(?:[\s-]\w+){0,2})"
_OBJECT = rf"(?:{_PRONOUN}|{_NOUN_PHRASE})"
_AP = r"(?:'|’)?"  # optional apostrophe, straight or curly

COMMITMENT_PATTERNS = [
    re.compile(rf"\bI{_AP}ll work on {_OBJECT} {_IMMEDIATE}\b", re.I),
    re.compile(
        rf"\bLet me (?:do|handle|work on|tackle|check on|look at|investigate) "
        rf"(?:{_PRONOUN}(?: {_IMMEDIATE})?|{_NOUN_PHRASE} {_IMMEDIATE})"
        rf"\b\s*[.!]?\s*$",
        re.I | re.M,
    ),
    re.compile(
        rf"\b(?:I{_AP}m|I am) "
        r"(?:doing|going to do|handling|going to handle|going to work on) "
        rf"{_OBJECT} {_IMMEDIATE}\b",
        re.I,
    ),
    re.compile(rf"\bDoing {_OBJECT} {_IMMEDIATE}\b", re.I),
    re.compile(
        rf"\bI{_AP}ll (?:handle|tackle|start|begin) "
        rf"(?:{_PRONOUN}(?: {_IMMEDIATE})?|{_NOUN_PHRASE} {_IMMEDIATE})"
        rf"\b\s*[.!]?\s*$",
        re.I | re.M,
    ),
    re.compile(
        rf"\bI{_AP}ll (?:get to|get on|look at|check on|investigate) "
        rf"(?:{_PRONOUN}(?: {_IMMEDIATE})?|{_NOUN_PHRASE} {_IMMEDIATE})"
        rf"\b\s*[.!]?\s*$",
        re.I | re.M,
    ),
    re.compile(rf"\bStarting {_OBJECT} {_IMMEDIATE}\b", re.I),
    re.compile(r"^\s*On it[.!]?\s*$", re.I | re.M),
    re.compile(
        r"(?:^|[.!?,;:\-–—]\s+)(?:right away|immediately|asap|shortly|"
        r"in a (?:bit|moment|sec|second|minute))[.!]?\s*$",
        re.I | re.M,
    ),
]


def strip_blockquotes_and_fences(text: str) -> str:
    """Strip fenced/inline code, quoted spans, and blockquotes so an agent
    quoting an EXAMPLE deferral in its prose doesn't self-trigger."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]+`", "", text)
    text = re.sub(r'"[^"\n]+"', "", text)
    text = re.sub(r"“[^”\n]+”", "", text)
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    return "\n".join(lines)


def read_last_user_and_assistant(transcript_path: Path, last_n_lines: int = 200):
    """Return (last_user_text, last_assistant_text, last_user_id) from the
    tail of a JSONL transcript. last_user_id identifies the most recent user
    turn (uuid/id if present, else a hash of its text) so the extension
    counter can tell 'still working the same request' apart from 'a new
    message arrived, start counting again'."""
    if not transcript_path.exists():
        return "", "", ""

    try:
        lines = transcript_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return "", "", ""

    last_user_text = ""
    last_assistant_text = ""
    last_user_id = ""

    for raw in lines[-last_n_lines:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        role = obj.get("role")
        content = obj.get("content")
        if not role and isinstance(obj.get("message"), dict):
            inner = obj["message"]
            role = inner.get("role")
            content = inner.get("content")

        if not role:
            continue

        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p)

        if not text:
            continue

        if role == "user":
            last_user_text = text
            uid = obj.get("uuid") or obj.get("id")
            if not uid:
                uid = hashlib.sha1(text.encode()).hexdigest()[:16]
            last_user_id = str(uid)
        elif role == "assistant":
            last_assistant_text = text

    return last_user_text, last_assistant_text, last_user_id


def load_counter(session_id: str, counter_file: Path = None) -> dict:
    """Load per-session counter state. Never raises; missing/corrupt data
    returns the zero state."""
    default = {"count": 0, "user_id": None}
    counter_file = counter_file if counter_file is not None else COUNTER_FILE
    if not counter_file.exists():
        return dict(default)
    try:
        with open(counter_file, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        entry = data.get(session_id, default) if isinstance(data, dict) else default
        if not isinstance(entry, dict):
            return dict(default)
        return {"count": int(entry.get("count", 0)), "user_id": entry.get("user_id")}
    except Exception:
        return dict(default)


def save_counter(session_id: str, state: dict, counter_file: Path = None) -> None:
    """Save per-session counter under an exclusive flock. Best-effort; never
    raises — a failed save just means the cap resets to 0 next time, which
    fails toward extending (safe) rather than toward being stuck blocked."""
    counter_file = counter_file if counter_file is not None else COUNTER_FILE
    try:
        counter_file.parent.mkdir(parents=True, exist_ok=True)
        with open(counter_file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                data[session_id] = state
                f.seek(0)
                f.truncate()
                json.dump(data, f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass


def continuation_prompt(match: re.Match, tail: str, count: int) -> str:
    start = max(0, match.start() - 40)
    end = min(len(tail), match.end() + 40)
    quote = tail[start:end].strip()
    return (
        f'You committed in your previous reply:\n\n'
        f'"{quote}"\n\n'
        f'Continue with that work now. Do not re-acknowledge the commitment, '
        f'do not restate it, do not explain that you intend to do it — execute it. '
        f'If the work requires tools, call them. If you cannot proceed '
        f'(missing input, blocked dependency), state the specific blocker in one line.\n\n'
        f'(Automated turn extension {count}/{MAX_EXTENSIONS} to enforce immediate commitments.)'
    )


def run_prepass(
    session_id: str,
    transcript_path: Path,
    stop_hook_active: bool = False,
    counter_file: Path = None,
) -> dict | None:
    """Testable entry point. Returns {"decision": "block", "reason": ...} to
    extend the turn, or None to let it end normally."""
    if stop_hook_active:
        # The harness already forced one continuation this stop cycle;
        # independent backstop against looping even if the on-disk counter
        # were lost or reset.
        return None

    if not transcript_path or not str(transcript_path):
        return None

    _user_text, asst_text, user_id = read_last_user_and_assistant(transcript_path)
    if not asst_text:
        return None

    asst_clean = strip_blockquotes_and_fences(asst_text)
    tail = asst_clean[-TAIL_CHARS:]

    state = load_counter(session_id, counter_file)
    if state.get("user_id") != user_id:
        state = {"count": 0, "user_id": user_id}

    for pattern in COMMITMENT_PATTERNS:
        m = pattern.search(tail)
        if not m:
            continue
        if state["count"] >= MAX_EXTENSIONS:
            return None  # cap hit; let the turn end
        state["count"] += 1
        save_counter(session_id, state, counter_file)
        return {
            "decision": "block",
            "reason": continuation_prompt(m, tail, state["count"]),
        }

    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    session_id = payload.get("session_id") or ""
    transcript_path_str = payload.get("transcript_path") or ""
    stop_hook_active = bool(payload.get("stop_hook_active"))

    if not transcript_path_str:
        return 0

    result = run_prepass(
        session_id=session_id,
        transcript_path=Path(transcript_path_str),
        stop_hook_active=stop_hook_active,
    )
    if result is not None:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
