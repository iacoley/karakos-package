"""Optional Discord behaviours (step 6.2). Pure and stdlib only.

Five switches, read from config/channels.json. Four are off by default; the
fifth, `tool_streaming`, is a per-channel override of the agent's own setting
(channel > agent `tool_streaming` in agents.yaml > default True):

    {"ux": {"suppress_embeds": true},
     "channels": {"general": {"id": "...", "ux": {"threads": {"after_s": 60},
                                                   "edit_reroute": true,
                                                   "reaction_notices": "owner"}}}}

A channel's "ux" object overrides the top-level one key by key; a key set in
neither is off (`tool_streaming`: inherits the agent's setting). A wrong type or an unknown key warns once (in `parse_ux`'s
warnings) and is treated as off.
"""
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import post_guard

# Discord message flag 1 << 2. tests/test_discord_ux.py pins it to discord.py.
SUPPRESS_EMBEDS = 4

KEYS = ("threads", "reaction_notices", "edit_reroute", "suppress_embeds", "tool_streaming")

THREAD_AFTER_S = 60
THREAD_MAX_LINES = 40
EDIT_WINDOW_S = 900
EDIT_MAX_FOLLOWUPS = 3

THREAD_NAME_PREFIX = "Working on: "
THREAD_NAME_BODY = 60
ZWSP = "​"


@dataclass(frozen=True)
class ThreadCfg:
    after_s: float = THREAD_AFTER_S
    max_lines: int = THREAD_MAX_LINES


@dataclass(frozen=True)
class EditCfg:
    window_s: float = EDIT_WINDOW_S
    max_followups: int = EDIT_MAX_FOLLOWUPS


@dataclass(frozen=True)
class ChannelUx:
    threads: Optional[ThreadCfg] = None
    reaction_notices: Optional[str] = None      # None | "owner" | "humans"
    edit_reroute: Optional[EditCfg] = None
    suppress_embeds: bool = False
    tool_streaming: Optional[bool] = None       # None: inherit the agent's setting


_UNSET = object()


def _num(v, positive=True):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (v > 0 or not positive)


def _parse_threads(v):
    if v is False:
        return None
    if v is True:
        return ThreadCfg()
    if isinstance(v, dict):
        extra = set(v) - {"after_s", "max_lines"}
        if extra:
            raise ValueError(f"unknown key(s) {sorted(extra)}")
        after = v.get("after_s", THREAD_AFTER_S)
        lines = v.get("max_lines", THREAD_MAX_LINES)
        if not _num(after, positive=False) or after < 0 or not _num(lines) or int(lines) != lines:
            raise ValueError("after_s must be a number >= 0 and max_lines a positive integer")
        return ThreadCfg(float(after), int(lines))
    raise ValueError("expected false, true or an object")


def _parse_reactions(v):
    if v is False:
        return None
    if v is True or v == "owner":
        return "owner"
    if v == "humans":
        return "humans"
    raise ValueError('expected false, true, "owner" or "humans"')


def _parse_edit(v):
    if v is False:
        return None
    if v is True:
        return EditCfg()
    if isinstance(v, dict):
        extra = set(v) - {"window_s", "max_followups"}
        if extra:
            raise ValueError(f"unknown key(s) {sorted(extra)}")
        win = v.get("window_s", EDIT_WINDOW_S)
        mx = v.get("max_followups", EDIT_MAX_FOLLOWUPS)
        if not _num(win) or not _num(mx) or int(mx) != mx:
            raise ValueError("window_s must be a positive number and max_followups a positive integer")
        return EditCfg(float(win), int(mx))
    raise ValueError("expected false, true or an object")


def _parse_bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("expected a boolean")


def _parse_tool_streaming(v):
    if isinstance(v, bool):
        return v
    raise ValueError("must be true or false")


_PARSERS = {
    "threads": _parse_threads,
    "reaction_notices": _parse_reactions,
    "edit_reroute": _parse_edit,
    "suppress_embeds": _parse_bool,
    "tool_streaming": _parse_tool_streaming,
}
_OFF = {"threads": None, "reaction_notices": None, "edit_reroute": None,
        "suppress_embeds": False, "tool_streaming": None}


def _parse_block(block, where: str, warnings: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if block is None:
        return out
    if not isinstance(block, dict):
        warnings.append(f"{where}: ux must be an object; ignored (off)")
        return out
    for key, value in block.items():
        if key not in _PARSERS:
            warnings.append(f"{where}: unknown ux key {key!r}; ignored")
            continue
        try:
            out[key] = _PARSERS[key](value)
        except ValueError as e:
            warnings.append(f"{where}: ux.{key}: {e}; off")
            out[key] = _OFF[key]
    return out


class UxConfig:
    def __init__(self, top: Dict[str, Any], channels: Dict[str, Dict[str, Any]]):
        self._top = top
        self._channels = channels

    def for_channel(self, name: Optional[str]) -> ChannelUx:
        over = self._channels.get(name, {}) if name else {}
        vals = {}
        for key in KEYS:
            if key in over:
                vals[key] = over[key]
            else:
                vals[key] = self._top.get(key, _OFF[key])
        return ChannelUx(**vals)

    def any_on(self) -> bool:
        names = [None] + list(self._channels)
        return any(c.threads or c.reaction_notices or c.edit_reroute or c.suppress_embeds
                   for c in (self.for_channel(n) for n in names))


def parse_ux(channels_config) -> Tuple[UxConfig, List[str]]:
    """Validate the "ux" blocks of a channels.json dict. Never raises."""
    warnings: List[str] = []
    if not isinstance(channels_config, dict):
        return UxConfig({}, {}), warnings
    top = _parse_block(channels_config.get("ux"), "channels.json", warnings)
    chans: Dict[str, Dict[str, Any]] = {}
    channels = channels_config.get("channels")
    if isinstance(channels, dict):
        for name, cfg in channels.items():
            if isinstance(cfg, dict) and "ux" in cfg:
                chans[name] = _parse_block(cfg.get("ux"), f"channel {name}", warnings)
    return UxConfig(top, chans), warnings


class LruMap:
    """Small in-memory thread-id -> parent-channel-id map."""

    def __init__(self, cap: int = 256):
        self.cap = cap
        self._d: "OrderedDict[str, str]" = OrderedDict()

    def __setitem__(self, key, value):
        key = str(key)
        self._d[key] = str(value)
        self._d.move_to_end(key)
        while len(self._d) > self.cap:
            self._d.popitem(last=False)

    def __getitem__(self, key):
        key = str(key)
        value = self._d[key]
        self._d.move_to_end(key)
        return value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        return str(key) in self._d

    def __len__(self):
        return len(self._d)


class LongTurn:
    """Per-turn state for long-turn threads. One per turn, created only when
    the switch is on for the turn's channel.

    The first tool line posts in the channel and its message id is the anchor.
    Once the turn has run `after_s` seconds and another line is due, the server
    creates a thread on the anchor and later lines go to the thread, with the
    per-turn cap raised to `max_lines`. A failed creation turns threading off
    for the rest of the turn.
    """

    def __init__(self, cfg: ThreadCfg, channel_id: str, start: float,
                 first_text: str, min_interval: float, base_cap: int):
        self.cfg = cfg
        self.channel_id = str(channel_id)
        self.start = start
        self.name = thread_name(first_text)
        self.min_interval = min_interval
        self.base_cap = base_cap
        self.anchor: Optional[str] = None
        self.thread_id: Optional[str] = None
        self.disabled = False

    @property
    def target(self) -> str:
        return self.thread_id or self.channel_id

    def plan(self, now: float, lines_posted: int, last_at: Optional[float]) -> str:
        """"skip", "post" (to `target`), or "thread" (create it, then post)."""
        can_thread = (self.thread_id is None and not self.disabled
                      and self.anchor is not None
                      and now - self.start >= self.cfg.after_s)
        cap = self.cfg.max_lines if (self.thread_id or can_thread) else self.base_cap
        if lines_posted >= cap:
            return "skip"
        if last_at is not None and now - last_at < self.min_interval:
            return "skip"
        return "thread" if can_thread else "post"

    def note_post(self, message_id: Optional[str]) -> None:
        if message_id and self.anchor is None and self.thread_id is None:
            self.anchor = str(message_id)

    def thread_created(self, thread_id: str) -> None:
        self.thread_id = str(thread_id)

    def thread_failed(self) -> None:
        self.disabled = True


# -- text builders ----------------------------------------------------------

def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def thread_name(batch_text: str) -> str:
    """`Working on: <first 60 chars>`; newlines flattened, @ made inert, never
    empty, never over Discord's 100 character limit."""
    body = _flat(batch_text)[:THREAD_NAME_BODY].replace("@", "@" + ZWSP).strip()
    if not body:
        body = "your request"
    return (THREAD_NAME_PREFIX + body)[:100]


def reaction_notice_text(reactor: str, emoji: str, snippet: str) -> str:
    snip = _flat(snippet)
    if len(snip) > 140:
        snip = snip[:139].rstrip() + "…"
    return ("[reaction, no reply needed unless it changes something] "
            f'{reactor} reacted {emoji} to your message: "{snip}"')


def edit_followup_text(editor: str, before: str, after: str, phase: str) -> str:
    when = "while you were working on it" if phase == "during" else "after you answered it"
    return (f"[{editor} edited their earlier message {when}]\n"
            f"Before: {str(before or '')[:500]}\n"
            f"After: {str(after or '')[:1000]}")


# -- edit decisions -----------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: str               # unknown|author_mismatch|too_old|unchanged|update|followup|
                              # update_followup|ignored|refused
    phase: Optional[str] = None            # "during" | "after" for followups
    followup_id: Optional[str] = None      # message_id of the queued follow-up to update

    @property
    def status(self) -> str:
        return {"update": "updated", "followup": "followup",
                "update_followup": "updated"}.get(self.action, self.action)


# Mirrors bin/agent-server.py STATUS_*; duplicated because this module is pure.
_QUEUED, _IN_PROGRESS = 0, 1


def edit_decision(row, now, window_s, max_followups, followups,
                  author_id=None, new_text=None) -> Decision:
    """Decide what an edit of a queued message means.

    `row` is the original message_queue row as a dict (None if unknown) with
    author_id, content, processed, response and `created_at_ts` (epoch
    seconds); `followups` its existing follow-up rows (dicts with message_id
    and processed), oldest first.
    """
    if row is None:
        return Decision("unknown")
    if author_id is not None and str(author_id) != str(row.get("author_id")):
        return Decision("author_mismatch")
    if now - float(row.get("created_at_ts") or 0) > window_s:
        return Decision("too_old")
    if new_text is not None and new_text == row.get("content"):
        return Decision("unchanged")

    state = row.get("processed")
    if state == _QUEUED:
        return Decision("update")
    if state == _IN_PROGRESS:
        phase = "during"
    else:
        if not post_guard.post_decision(row.get("response") or "")[0]:
            return Decision("ignored")
        phase = "after"
    for f in reversed(followups or []):
        if f.get("processed") == _QUEUED:
            return Decision("update_followup", phase, f.get("message_id"))
    if len(followups or []) >= max_followups:
        return Decision("refused")
    return Decision("followup", phase)
