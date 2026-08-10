#!/usr/bin/env python3
"""
Ask-the-user bridge — the Discord surface for a multiple-choice question.

Why this exists at all
----------------------
Claude Code's built-in `AskUserQuestion` tool does not exist over this
transport. The agents here are spawned as `claude -p --input-format
stream-json`, and in that mode the CLI does not put `AskUserQuestion` in the
session's tool list — verified against claude 2.1.220 by reading the `tools`
array off the `system`/`init` event, with and without
`--allowedTools AskUserQuestion`. So there is nothing to intercept in the
output stream: the model is never offered the tool in the first place.

The bridge is therefore a *replacement* tool, not an interception. The MCP
tool server exposes `ask_user`; it calls the agent server, which renders the
question into this module's Discord payload (an embed plus one button per
option) and parks it here; the relay — the only process holding a Discord
gateway connection — receives the button click and hands the choice back.

This module is deliberately free of I/O and of aiohttp/discord imports so
that all three processes can share it and so the payload shape and the
registry state machine can be tested without a network.
"""

import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Buttons carry the correlation id back to us: Discord echoes `custom_id`
# verbatim on the interaction and gives us nothing else to route on. Kept
# short because Discord caps custom_id at 100 characters.
CUSTOM_ID_PREFIX = "kask"

# Discord ceilings, not preferences: 5 buttons to an action row, 80
# characters to a button label, and 5 action rows to a message. Two rows is
# as many choices as anyone can usefully be handed at once.
BUTTONS_PER_ROW = 5
MAX_OPTIONS = 10
MAX_LABEL_LEN = 80
MAX_DESCRIPTION_LEN = 100
MAX_QUESTION_LEN = 2000
MAX_HEADER_LEN = 256

EMBED_COLOR = 0x5865F2
ANSWERED_COLOR = 0x57F287
EXPIRED_COLOR = 0x99AAB5

# How long a question stays clickable. Past this the registry reports
# `expired` and the caller gets its turn back rather than blocking forever on
# a user who has gone to bed.
DEFAULT_TIMEOUT_SEC = 300.0
MIN_TIMEOUT_SEC = 10.0
MAX_TIMEOUT_SEC = 3600.0

# Beacon state written while a question is outstanding. bin/wedge-check.py
# only treats PROCESSING and ERROR_RECOVERY as "a turn was claimed", so
# parking the beacon in this state stops a person taking four minutes to
# decide from being paged as a wedged agent — without also making a genuinely
# hung agent invisible, because the state flips back the moment the ask
# resolves or expires.
AWAITING_USER_STATE = "AWAITING_USER"


class AskError(ValueError):
    """A question that cannot be rendered — bad option list, empty prompt."""


def clamp_timeout(value: Any, default: float = DEFAULT_TIMEOUT_SEC) -> float:
    """Coerce a caller-supplied timeout into the supported range."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds:  # NaN
        return default
    return max(MIN_TIMEOUT_SEC, min(MAX_TIMEOUT_SEC, seconds))


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_options(options: Any) -> List[Dict[str, str]]:
    """Accept `["a", "b"]` or `[{"label": ..., "description": ...}]`.

    Raises AskError rather than silently dropping choices: a question whose
    options were quietly mangled is worse than one that fails loudly, because
    the agent would act on an answer to a question the user never saw.
    """
    if not isinstance(options, (list, tuple)):
        raise AskError("options must be a list")
    if not options:
        raise AskError("options must not be empty")
    if len(options) > MAX_OPTIONS:
        raise AskError(f"at most {MAX_OPTIONS} options are supported")

    normalized: List[Dict[str, str]] = []
    seen = set()
    for raw in options:
        if isinstance(raw, str):
            label, description = raw, ""
        elif isinstance(raw, dict):
            label = raw.get("label") or raw.get("name") or ""
            description = raw.get("description") or ""
        else:
            raise AskError(f"option must be a string or object, got {type(raw).__name__}")

        label = _truncate(label, MAX_LABEL_LEN)
        if not label:
            raise AskError("every option needs a non-empty label")
        if label.lower() in seen:
            raise AskError(f"duplicate option label: {label}")
        seen.add(label.lower())
        normalized.append({
            "label": label,
            "description": _truncate(description, MAX_DESCRIPTION_LEN),
        })
    return normalized


def new_ask_id() -> str:
    return uuid.uuid4().hex[:12]


def build_custom_id(ask_id: str, index: int) -> str:
    return f"{CUSTOM_ID_PREFIX}:{ask_id}:{index}"


def parse_custom_id(custom_id: Any) -> Optional[Tuple[str, int]]:
    """Inverse of build_custom_id. None for anything that isn't ours.

    Every component interaction in the guild arrives at the relay, including
    ones belonging to other features, so this has to reject cleanly rather
    than raise."""
    if not isinstance(custom_id, str):
        return None
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[0] != CUSTOM_ID_PREFIX or not parts[1]:
        return None
    try:
        index = int(parts[2])
    except ValueError:
        return None
    if index < 0:
        return None
    return parts[1], index


def build_ask_payload(
    ask_id: str,
    question: str,
    options: Sequence[Dict[str, str]],
    header: Optional[str] = None,
    agent: Optional[str] = None,
) -> Dict[str, Any]:
    """The Discord REST message body for an outstanding question."""
    question_text = _truncate(question, MAX_QUESTION_LEN)
    if not question_text:
        raise AskError("question must not be empty")

    fields = [
        {
            "name": f"{idx + 1}. {opt['label']}",
            "value": opt["description"] or "​",
            "inline": False,
        }
        for idx, opt in enumerate(options)
        if opt.get("description")
    ]

    embed: Dict[str, Any] = {
        "title": _truncate(header or "A question for you", MAX_HEADER_LEN),
        "description": question_text,
        "color": EMBED_COLOR,
    }
    if fields:
        embed["fields"] = fields
    if agent:
        embed["footer"] = {"text": f"asked by {agent}"}

    rows: List[Dict[str, Any]] = []
    for idx, opt in enumerate(options):
        if idx % BUTTONS_PER_ROW == 0:
            rows.append({"type": 1, "components": []})
        rows[-1]["components"].append({
            "type": 2,
            "style": 1,
            "label": opt["label"],
            "custom_id": build_custom_id(ask_id, idx),
        })

    return {"embeds": [embed], "components": rows}


class PendingAsk:
    """One outstanding question."""

    __slots__ = (
        "ask_id", "agent", "channel_id", "question", "options", "header",
        "created_at", "expires_at", "allowed_user_ids", "message_id",
        "answer_index", "answer_label", "answered_by", "answered_at", "expired",
    )

    def __init__(self, ask_id, agent, channel_id, question, options, header,
                 created_at, expires_at, allowed_user_ids):
        self.ask_id = ask_id
        self.agent = agent
        self.channel_id = channel_id
        self.question = question
        self.options = options
        self.header = header
        self.created_at = created_at
        self.expires_at = expires_at
        self.allowed_user_ids = set(allowed_user_ids or ())
        self.message_id: Optional[str] = None
        self.answer_index: Optional[int] = None
        self.answer_label: Optional[str] = None
        self.answered_by: Optional[str] = None
        self.answered_at: Optional[float] = None
        self.expired = False

    @property
    def answered(self) -> bool:
        return self.answer_index is not None

    def may_answer(self, user_id: Any) -> bool:
        """Empty allow-list means anybody in the channel; otherwise the people
        whose message started this turn (plus the owner) and nobody else. The
        answer is fed straight back into the agent's context, so an unlisted
        bystander steering a decision is a real hijack, not a nicety."""
        if not self.allowed_user_ids:
            return True
        return str(user_id) in self.allowed_user_ids

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        if self.answered:
            state = "answered"
        elif self.expired or now >= self.expires_at:
            state = "expired"
        else:
            state = "pending"
        return {
            "ask_id": self.ask_id,
            "status": state,
            "agent": self.agent,
            "question": self.question,
            "options": [o["label"] for o in self.options],
            "answer": self.answer_label,
            "answer_index": self.answer_index,
            "answered_by": self.answered_by,
            "message_id": self.message_id,
            "expires_in": max(0.0, round(self.expires_at - now, 3)),
        }


class AskRegistry:
    """In-memory home for outstanding questions, owned by the agent server.

    Deliberately not persisted: an unanswered question belongs to a live
    subprocess turn, and that turn does not survive a server restart either.
    A stale button after a restart resolves to `unknown`, which the relay
    reports to the clicker instead of feeding a stale answer to an agent that
    has forgotten it asked.
    """

    def __init__(self, default_timeout: float = DEFAULT_TIMEOUT_SEC):
        self.default_timeout = default_timeout
        self._asks: Dict[str, PendingAsk] = {}

    def __len__(self) -> int:
        return len(self._asks)

    def create(self, agent: str, channel_id: str, question: str, options: Any,
               header: Optional[str] = None, timeout: Optional[float] = None,
               allowed_user_ids: Optional[Sequence[str]] = None,
               now: Optional[float] = None) -> PendingAsk:
        now = time.time() if now is None else now
        normalized = normalize_options(options)
        question_text = _truncate(question, MAX_QUESTION_LEN)
        if not question_text:
            raise AskError("question must not be empty")
        seconds = clamp_timeout(timeout, self.default_timeout)
        ask = PendingAsk(
            ask_id=new_ask_id(),
            agent=agent,
            channel_id=str(channel_id),
            question=question_text,
            options=normalized,
            header=header,
            created_at=now,
            expires_at=now + seconds,
            allowed_user_ids=allowed_user_ids,
        )
        self._asks[ask.ask_id] = ask
        return ask

    def get(self, ask_id: str) -> Optional[PendingAsk]:
        return self._asks.get(ask_id)

    def payload_for(self, ask: PendingAsk) -> Dict[str, Any]:
        return build_ask_payload(
            ask.ask_id, ask.question, ask.options, ask.header, ask.agent
        )

    def answer(self, ask_id: str, index: int, user_id: Any = None,
               user_name: Optional[str] = None,
               now: Optional[float] = None) -> Tuple[str, Optional[PendingAsk]]:
        """Resolve a question. Returns (outcome, ask).

        Outcomes: answered / unknown / expired / already / forbidden /
        bad_index. Each maps to a distinct thing the clicker is told, because
        "nothing happened" is the failure mode that made the old
        no-Discord-surface behaviour so hard to diagnose.
        """
        now = time.time() if now is None else now
        ask = self._asks.get(ask_id)
        if ask is None:
            return "unknown", None
        if ask.answered:
            return "already", ask
        if ask.expired or now >= ask.expires_at:
            ask.expired = True
            return "expired", ask
        if not ask.may_answer(user_id):
            return "forbidden", ask
        if not isinstance(index, int) or not (0 <= index < len(ask.options)):
            return "bad_index", ask

        ask.answer_index = index
        ask.answer_label = ask.options[index]["label"]
        ask.answered_by = user_name or (str(user_id) if user_id is not None else None)
        ask.answered_at = now
        return "answered", ask

    def sweep(self, now: Optional[float] = None) -> List[PendingAsk]:
        """Drop resolved/expired questions. Returns the ones that expired
        without an answer, so the caller can put the agent's beacon back."""
        now = time.time() if now is None else now
        newly_expired = []
        for ask_id, ask in list(self._asks.items()):
            if ask.answered:
                # Answered asks are read once by the polling tool, then
                # collected on the next sweep after a grace window.
                if ask.answered_at is not None and now - ask.answered_at > 60:
                    self._asks.pop(ask_id, None)
                continue
            if now >= ask.expires_at:
                if not ask.expired:
                    ask.expired = True
                    newly_expired.append(ask)
                if now - ask.expires_at > 60:
                    self._asks.pop(ask_id, None)
        return newly_expired

    def discard_agent(self, agent: str) -> int:
        """Forget every question belonging to an agent whose turn has ended."""
        doomed = [k for k, v in self._asks.items() if v.agent == agent]
        for key in doomed:
            self._asks.pop(key, None)
        return len(doomed)


def resolution_note(outcome: str, ask: Optional[PendingAsk]) -> str:
    """One line to show the person who clicked."""
    if outcome == "answered" and ask is not None:
        return f"Answer recorded: **{ask.answer_label}**"
    if outcome == "already" and ask is not None:
        return f"Already answered: **{ask.answer_label}**"
    if outcome == "expired":
        return "This question timed out — the agent has stopped waiting."
    if outcome == "forbidden":
        return "This question was put to someone else."
    if outcome == "unknown":
        return "This question is no longer active."
    return "That option is not valid for this question."
