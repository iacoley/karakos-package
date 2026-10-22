#!/usr/bin/env python3
"""
Karakos Relay — Discord + Dispatch + Capture

Adapters:
- DiscordAdapter: Routes Discord messages to agent server
- DispatchAdapter: Watches inbox dirs, invokes builder/reviewer
- CaptureAdapter: Persists Discord messages to JSONL
"""

import asyncio
import discord
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Dict, List
from logging.handlers import RotatingFileHandler

# bin/ is a directory of scripts, not a package — see the same note in
# bin/agent-server.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# lib/ holds modules shared by more than one script; it sits beside bin/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import ask_handler  # noqa: E402
import build_dispatcher  # noqa: E402
import build_hosts  # noqa: E402
import channel_context  # noqa: E402
import discord_ux  # noqa: E402
import registry as agent_registry  # noqa: E402
import reply_classifier  # noqa: E402
import reply_gate_config  # noqa: E402
import routing  # noqa: E402
import tengwar  # noqa: E402

# =============================================================================
# Utilities
# =============================================================================

def split_discord_message(text: str, max_length: int = 2000) -> List[str]:
    """Split text into chunks Discord will accept (max 2000 chars each).

    Delegates to tengwar.split_for_discord, which cuts on line/space
    boundaries (hard cut when there are none), defers a whole fenced block to
    the next chunk when it fits, and closes/reopens ``` fences across every
    cut so no chunk carries a dangling fence. Discord rejects anything over
    2000 with a 400 and the message is lost, so the result is size-checked.
    """
    if len(text) <= max_length:
        return [text] if text else []

    # Headroom for the "\n```" balance_fences appends and the "```lang\n"
    # it prepends to the next chunk.
    chunks = tengwar.split_for_discord(text, max_len=max_length - 100)
    safe: List[str] = []
    for chunk in chunks:
        while len(chunk) > max_length:  # pathological fence info string
            safe.append(chunk[:max_length])
            chunk = chunk[max_length:]
        if chunk:
            safe.append(chunk)
    return safe if safe else [text]


# =============================================================================
# System commands
# =============================================================================

# Commands the relay handles itself instead of forwarding to an agent. Kept
# deliberately small: these three are the ones you need when an agent is
# already too wedged to read its own messages, which is exactly when shell
# access is least convenient.
SYS_COMMANDS = frozenset({"clear", "reload", "status", "usage"})

# Commands registered as real Discord application commands by
# bin/register-discord-commands.py and dispatched by
# `DiscordAdapter.on_interaction`. This is deliberately a superset of
# SYS_COMMANDS: an application command arrives already distinguished from a
# sentence, so it costs nothing to expose, while every name added to
# SYS_COMMANDS is one more word the relay swallows out of ordinary chat.
#
# Keep in step with COMMANDS in bin/register-discord-commands.py. A name here
# with no registration is unreachable; a registration with no name here is
# dropped by on_interaction. tests/test_relay_slash_commands.py fails the
# build on either.
SLASH_COMMANDS = frozenset({
    "status", "health", "usage", "help",
    "cost", "clear", "reload", "interrupt", "kill", "flush",
    "logs", "pause", "resume", "effort", "model",
})

INTERRUPT_MESSAGE_MAX = 1900
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "default")

# Commands that report on the whole install rather than one agent, so they
# neither need nor accept a target and must not be blocked by "which agent?".
UNTARGETED_COMMANDS = frozenset({"status", "health", "usage", "help", "logs"})

# /logs — a service name becomes a path segment under logs/, so it is
# validated rather than sanitised: `..` and `/` have no valid form here.
_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LOG_LINES_DEFAULT = 40
# Discord's own ceiling is 2000 characters per message and split_discord_message
# will chunk beyond that; this stops one command from producing twenty posts.
LOG_LINES_MAX = 200

# <@123>, <@!123> (nickname form), <@&123> (role).
_MENTION_RE = re.compile(r"<@[!&]?\d+>")


def slash_args(cmd: str, options: Dict) -> str:
    """Rebuild the free-text `args` string a `handle_sys_command` branch parses.

    Slash commands arrive as typed options; every branch of the handler was
    written against a string, and the text surface still delivers one.
    Reassembling here keeps one implementation instead of two, at the cost of
    this small translation table.
    """
    def s(key: str) -> str:
        value = (options or {}).get(key)
        return "" if value is None else str(value).strip()

    if cmd == "logs":
        return " ".join(p for p in (s("service"), s("lines")) if p)
    if cmd == "pause":
        return s("minutes")
    if cmd == "effort":
        return s("level")
    if cmd == "model":
        return s("model")
    if cmd == "interrupt":
        return s("message")
    # Everything else is either untargeted or agent-targeted, and the target
    # travels as `mentioned` (the same slot a text-path @mention fills), not
    # as args — so there is nothing left to rebuild.
    return ""


def strip_mentions(text: str) -> str:
    """Drop Discord mention tokens so `@Agent /sys clear` matches as a command."""
    return _MENTION_RE.sub(" ", text or "").strip()


# =============================================================================
# Attachments
# =============================================================================

# Anything outside this set is replaced. That covers `/` and `\` — an uploader
# controls the filename, and a name like `../../config/agents.yaml` must not be
# able to choose where the relay writes.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_attachment_name(filename: str, index: int) -> str:
    """Return a filesystem-safe name for a Discord-supplied filename.

    The index prefix is not decoration: two attachments on one message may
    share a filename, and without it the second silently overwrites the first
    and the agent is handed the same bytes twice.
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", filename or "")
    # Leading dots are stripped so a name of `..` or `.` cannot survive as a
    # path component, and so uploads do not land as dotfiles.
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        cleaned = "attachment"
    # Long names are truncated from the front, keeping the tail so the
    # extension (which is how the agent knows it is an image) survives.
    if len(cleaned) > 96:
        cleaned = cleaned[-96:]
    return f"{index}-{cleaned}"


REPLY_CONTEXT_MAX_CHARS = 1000


def server_label(obj) -> str:
    """Guild name for a Discord channel or message, "dm" outside a guild.

    Queue rows store this as `server`, and the edit lookup matches on it, so
    every relay path that names a message must use this one helper."""
    guild = getattr(obj, "guild", None)
    return guild.name if guild else "dm"


def format_reply_context(target, max_chars: int = REPLY_CONTEXT_MAX_CHARS) -> str:
    """Render the replied-to message as a delimited block for the agent.

    `target` is a discord message-like object, or None when the reference
    could not be resolved (deleted, or not fetchable).
    """
    author = getattr(target, "author", None) if target is not None else None
    if target is None or author is None:
        return "[In reply to a message that is no longer available]"
    name = getattr(author, "display_name", None) or getattr(author, "name", None) or "unknown"
    text = (getattr(target, "content", None) or "").strip()
    if not text:
        atts = getattr(target, "attachments", None) or []
        text = ("[attachments: " + ", ".join(a.filename for a in atts) + "]") if atts else "[no text content]"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "… [truncated]"
    return f"[In reply to {name}:\n{text}\n]"


def parse_sys_command(content: str):
    """Return (command, args) if `content` is a system command, else None.

    Accepts both `/clear` and the older `/sys clear` — the prefix existed so a
    single handler could tell a command from a sentence, and it stays because
    it is still in people's fingers. Anything that is not a recognised command
    returns None and falls through to normal agent routing, so a message that
    merely opens with a slash is not swallowed.
    """
    bare = strip_mentions(content)
    if not bare.startswith("/"):
        return None

    parts = bare[1:].split(None, 1)
    if not parts:
        return None
    head = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if head == "sys":
        parts = rest.split(None, 1)
        head = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

    if head in SYS_COMMANDS:
        return (head, rest)
    return None


def resolve_target_agent(mentioned, channel_default, known_agents):
    """Pick which agent a system command acts on.

    Order is mentioned > channel default > the only agent there is. The last
    rung is the point: with two or more agents and nothing naming one, this
    returns an error rather than picking. A system command that guesses its
    target clears the wrong agent's session, and the person typing it has no
    way to tell that happened.
    """
    known = list(known_agents or [])

    if mentioned:
        if mentioned not in known:
            return None, f"Unknown agent `{mentioned}`."
        return mentioned, None

    if channel_default:
        if channel_default not in known:
            return None, f"This channel's default agent `{channel_default}` is not configured."
        return channel_default, None

    if len(known) == 1:
        return known[0], None

    if not known:
        return None, "No agents are configured."

    names = ", ".join(f"`{a}`" for a in sorted(known))
    return None, f"Which agent? Mention one — this channel has no default. Configured: {names}"


# =============================================================================
# Reply gate — who is a message actually for?
# =============================================================================

# Above this many messages/minute from humans, assume the humans are talking to
# each other rather than to us.
VOLLEY_WINDOW_SEC = 90
VOLLEY_MSGS_PER_MIN = 4.0
# After speaking, we count as a participant for this long, and the volley rule
# stops applying — otherwise we go mute exactly when a conversation we are part
# of gets lively.
PARTICIPATION_SEC = 240

_NAME_OPENER_CACHE: Dict[str, "re.Pattern"] = {}


def _name_opener(agent_name: str):
    """`Amos, can you...` — a message that opens with the agent's name."""
    key = (agent_name or "").lower()
    if key not in _NAME_OPENER_CACHE:
        _NAME_OPENER_CACHE[key] = re.compile(
            r"^\s*(hey\s+|ok(?:ay)?\s+)?" + re.escape(key) + r"\b[\s,:!?]*", re.I
        )
    return _NAME_OPENER_CACHE[key]


class ReplyGate:
    """Decide whether a message in a shared human channel is addressed to us.

    Only channels configured with `"reply_gate": true` are gated; everywhere
    else behaviour is unchanged, so an install that never opts in sees exactly
    what it saw before.

    Two tiers, silence-biased:

      Tier 1, deterministic and always wins:
          @mention of one of our agents ....... ENGAGE
          reply to one of our agents' messages  ENGAGE
          message opens with an agent's name .. ENGAGE
          reply to a different human .......... SILENT

      Volley: humans trading messages quickly are talking to each other, so
      above the rate threshold we stay quiet — unless we are already a
      participant (we spoke recently in that channel).

    Anything still unresolved returns ASK, meaning "a classifier could decide
    this". There is no classifier here, and the relay treats ASK as silence:
    an unwanted "was that for me?" costs the humans their conversation, while
    silence costs one word to recover from. ASK is kept distinct from SILENT so
    an install can hook a classifier in without reworking the tiers.

    Opt-in tier 2: a channel whose `reply_gate` is an object with
    `"classifier": "haiku"` has ASK decided by a small model (`tier2`). Any
    doubt, error, timeout or limit keeps the silence. Channels that do not opt
    in never reach it.
    """

    CONTEXT_RING = 20

    def __init__(self):
        # channel id -> {"human_msgs": [ts], "last_post": ts}
        self._channels: Dict[int, Dict] = {}
        # In-memory only, never logged or written: channel id -> recent messages.
        self._ring: Dict[int, "deque"] = {}
        self._calls: Dict[int, "deque"] = {}      # channel id -> classifier call times
        self._chan_locks: Dict[int, tuple] = {}   # channel id -> (loop, Lock)
        self._sem = None                          # (loop, Semaphore(2))
        self.runner = None                        # injectable; default run_claude
        self.stats = {"calls": 0, "engaged": 0, "silent": 0, "errors": 0,
                      "timeouts": 0, "rate_capped": 0, "budget": 0, "last_call_ts": None}
        self._last_budget_warn = 0.0

    def note_message(self, channel_id, author_name, text, is_agent=False, now=None):
        ring = self._ring.setdefault(int(channel_id), deque(maxlen=self.CONTEXT_RING))
        ring.append((str(author_name or ""), str(text or "")[:300], bool(is_agent),
                     now if now is not None else time.time()))

    def recent(self, channel_id, n):
        """The last `n` messages as [(author, text)], oldest first."""
        if n <= 0:
            return []
        return [(a, t) for a, t, _ag, _ts in list(self._ring.get(int(channel_id), ()))[-n:]]

    def _loop_bound(self, store, key, factory):
        loop = asyncio.get_running_loop()
        entry = store.get(key)
        if entry is None or entry[0] is not loop:
            entry = (loop, factory())
            store[key] = entry
        return entry[1]

    async def tier2(self, *, channel_id, cfg, agent_names, author_name, content,
                    db_path=None, on_cost=None, now=None):
        """Classify an ASK. Returns (verdict, reason); never raises."""
        cid = int(channel_id)
        deadline = time.monotonic() + cfg.timeout_s
        try:
            lock = self._loop_bound(self._chan_locks, cid, asyncio.Lock)
            try:
                await asyncio.wait_for(lock.acquire(), timeout=cfg.timeout_s)
            except asyncio.TimeoutError:
                self.stats["timeouts"] += 1
                return ("silent", "haiku timeout")
            try:
                return await self._tier2_locked(
                    cid, cfg, agent_names, author_name, content, db_path, on_cost,
                    deadline, now)
            finally:
                lock.release()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("tier2 error: %s", e)
            self.stats["errors"] += 1
            return ("silent", "haiku error")

    async def _tier2_locked(self, cid, cfg, agent_names, author_name, content,
                            db_path, on_cost, deadline, now):
        if self._sem is None or self._sem[0] is not asyncio.get_running_loop():
            self._sem = (asyncio.get_running_loop(), asyncio.Semaphore(2))
        async with self._sem[1]:
            if db_path is not None and await asyncio.to_thread(
                    reply_classifier.account_paused, db_path):
                return ("silent", "haiku account_paused")

            t = time.time() if now is None else now
            calls = self._calls.setdefault(cid, deque())
            while calls and calls[0] <= t - 3600:
                calls.popleft()
            if len(calls) >= cfg.max_per_hour or sum(1 for c in calls if c > t - 60) >= cfg.max_per_minute:
                self.stats["rate_capped"] += 1
                return ("silent", "haiku rate_capped")
            calls.append(t)

            self.stats["calls"] += 1
            self.stats["last_call_ts"] = t
            v = await reply_classifier.classify(
                cfg, list(agent_names), self.recent(cid, cfg.context_messages),
                (author_name, content), runner=self.runner or reply_classifier.run_claude)

        if v.cost and on_cost is not None:
            try:
                await on_cost(v.cost)
            except Exception as e:  # noqa: BLE001
                log.debug("classifier cost post failed: %s", e)
        if v.reason == "timeout":
            self.stats["timeouts"] += 1
        elif v.reason in ("error", "exit", "empty"):
            self.stats["errors"] += 1
        elif v.reason == "budget":
            self.stats["budget"] += 1
            if time.time() - self._last_budget_warn > 3600:
                self._last_budget_warn = time.time()
                log.warning("classifier budget cap is below the per-call cost")
        if v.engage:
            self.stats["engaged"] += 1
            return ("engage", f"haiku {v.confidence:.2f}")
        self.stats["silent"] += 1
        return ("silent", f"haiku {v.reason}")

    def _state(self, channel_id: int) -> Dict:
        return self._channels.setdefault(channel_id, {"human_msgs": [], "last_post": 0.0})

    def note_agent_post(self, channel_id: int, now: Optional[float] = None):
        """Call whenever one of our agents posts, so participation stays accurate."""
        self._state(int(channel_id))["last_post"] = now if now is not None else time.time()

    def _rate(self, st: Dict, now: float) -> float:
        cutoff = now - VOLLEY_WINDOW_SEC
        st["human_msgs"] = [t for t in st["human_msgs"] if t >= cutoff]
        return len(st["human_msgs"]) * 60.0 / VOLLEY_WINDOW_SEC

    def decide(self, *, channel_id: int, content: str, mentions_agent: bool,
               replied_to_author_id: Optional[int], agent_ids: set,
               agent_names=(), now: Optional[float] = None):
        """Return (verdict, reason) where verdict is 'engage' | 'silent' | 'ask'."""
        now = now if now is not None else time.time()
        st = self._state(int(channel_id))
        st["human_msgs"].append(now)

        if mentions_agent:
            return ("engage", "@mention")
        if replied_to_author_id is not None and replied_to_author_id in agent_ids:
            return ("engage", "reply to agent")
        for name in agent_names or ():
            if name and _name_opener(name).match(content or ""):
                return ("engage", f"addressed by name ({name})")
        if replied_to_author_id is not None and replied_to_author_id not in agent_ids:
            # Replying to another human is the clearest not-for-me signal there is.
            return ("silent", "reply to another human")

        rate = self._rate(st, now)
        participating = (now - st["last_post"]) < PARTICIPATION_SEC
        if rate >= VOLLEY_MSGS_PER_MIN and not participating:
            return ("silent", f"volley {rate:.1f}/min, not participating")

        return ("ask", "ambiguous")


# =============================================================================
# Guest turn budget — two bots in one channel
# =============================================================================

# Two agents that each answer the other will not stop on their own. Routing to
# a bot already requires an explicit @mention, which makes a loop a deliberate
# act rather than an accident; this is the backstop for when it is deliberate.
# After this many bot messages with no human in between, we go quiet until a
# human speaks in the channel again.
GUEST_TURN_LIMIT = int(os.environ.get("GUEST_TURN_LIMIT", "12"))


class GuestBudget:
    """Bot-to-bot turns per channel since the last human message."""

    def __init__(self, limit: int = None):
        self.limit = GUEST_TURN_LIMIT if limit is None else limit
        self._turns: Dict[int, int] = {}
        self._announced: set = set()

    def take(self, channel_id: int):
        """Count one bot turn. Returns (allowed, turns, should_announce).

        `should_announce` is true exactly once per exhaustion — on the message
        that crosses the limit — so the notice explaining the stop cannot itself
        become the next runaway.
        """
        cid = int(channel_id)
        turns = self._turns.get(cid, 0) + 1
        self._turns[cid] = turns
        if turns > self.limit:
            announce = cid not in self._announced
            self._announced.add(cid)
            return (False, turns, announce)
        return (True, turns, False)

    def reset(self, channel_id: int):
        """A human spoke — the budget refills."""
        cid = int(channel_id)
        self._turns.pop(cid, None)
        self._announced.discard(cid)


# =============================================================================
# Configuration
# =============================================================================

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.yaml"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
MESSAGES_DIR = WORKSPACE_ROOT / "data" / "messages"
ATTACHMENTS_DIR = WORKSPACE_ROOT / "data" / "attachments"
# Inbound messages the agent server refused or never received spool here;
# bin/flush-deferred-messages.py (scheduler, every 5 minutes) re-fires them
# once the server is back (#88). Only transient failures spool — a 400 would
# be refused identically on every refire.
DEFERRED_MESSAGES_DIR = WORKSPACE_ROOT / "data" / "deferred-messages"
HEALTH_FILE = WORKSPACE_ROOT / "data" / "health" / "relay.json"
HEALTH_INTERVAL_S = 60

# Attachments the relay will pull down before handing a message to an agent.
# Discord's own ceiling is 25 MB on an unboosted server, so the default cap
# refuses nothing Discord would have accepted while still bounding what a
# single message can write to the data volume.
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "10"))

AGENT_SERVER_PORT = os.environ.get("AGENT_SERVER_PORT", "18791")
AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", f"http://localhost:{AGENT_SERVER_PORT}")
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
OWNER_DISCORD_ID = int(os.environ.get("OWNER_DISCORD_ID", "0"))

# Reaction notices (6.2): one per (user, message) per cooldown, and at most
# REACTION_NOTICE_RATE per channel per minute.
REACTION_NOTICE_COOLDOWN_S = 60
REACTION_NOTICE_RATE = 10

# Dispatch config
DISPATCH_INBOX_DIR = WORKSPACE_ROOT / "inbox"
DISPATCH_POLL_INTERVAL = 30
MAX_CONCURRENT_BUILDERS = int(os.environ.get("MAX_CONCURRENT_BUILDERS", "1"))
MAX_CONCURRENT_REVIEWERS = int(os.environ.get("MAX_CONCURRENT_REVIEWERS", "2"))
DISPATCH_TIMEOUTS = {
    "reviewer": 3600,    # 1 hour
    "builder": 21600,    # 6 hours
}

# Logging
log = logging.getLogger("relay")
log.setLevel(logging.INFO)
handler = RotatingFileHandler(
    WORKSPACE_ROOT / "logs" / "relay.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=7
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
log.addHandler(handler)

console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(console)

# Global state
agent_config: Dict = {}
channels_config: Dict = {}
registry_obj = None
discord_id_to_agent: Dict[int, str] = {}
discord_role_id_to_agent: Dict[int, str] = {}
active_dispatches: Dict[str, asyncio.Task] = {}
dispatch_semaphores: Dict[str, asyncio.Semaphore] = {}

def spool_deferred_message(payload: dict, reason: str) -> Optional[Path]:
    """Write a /message payload that could not be delivered to the spool.

    Returns the file path, or None — a spool failure must never take down
    on_message, so this swallows everything and settles for the error log
    the caller already wrote.
    """
    try:
        DEFERRED_MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        path = DEFERRED_MESSAGES_DIR / f"{int(time.time())}-{payload['message_id']}.json"
        path.write_text(json.dumps(payload))
        log.warning(
            f"Spooled message {payload['message_id']} for retry ({reason}): {path}"
        )
        return path
    except Exception as spool_err:
        log.error(f"Failed to spool deferred message: {spool_err}")
        return None


# =============================================================================
# Configuration Loading
# =============================================================================

def load_config():
    """Load agent and channel configuration"""
    global agent_config, channels_config, discord_id_to_agent, discord_role_id_to_agent, registry_obj

    # Load agents (config/agents.yaml via the registry; no legacy fallback)
    try:
        loaded = agent_registry.load_registry(WORKSPACE_ROOT)
    except agent_registry.RegistryError as e:
        log.error(f"Agent registry unusable: {e}")
        agent_config = {}
        registry_obj = None
    else:
        registry_obj = loaded
        agent_config = registry_obj.legacy_view()["agents"]

    # Load channels
    if CHANNELS_CONFIG_PATH.exists():
        with open(CHANNELS_CONFIG_PATH) as f:
            channels_config = json.load(f)
    else:
        channels_config = {}
        log.warning(f"Channels config not found: {CHANNELS_CONFIG_PATH}")

    # Build Discord ID map
    for agent_name, config in agent_config.items():
        bot_id_env = config.get("discord_bot_id_env")
        if bot_id_env:
            bot_id = os.environ.get(bot_id_env)
            if bot_id:
                discord_id_to_agent[int(bot_id)] = agent_name
        role_id_env = config.get("discord_role_id_env")
        if role_id_env:
            role_id = os.environ.get(role_id_env)
            if role_id:
                discord_role_id_to_agent[int(role_id)] = agent_name

    for name, cfg in (channels_config.get("channels") or {}).items():
        if isinstance(cfg, dict) and cfg.get("mention_only") is True and cfg.get("reply_gate"):
            log.error(f"channel '{name}' sets both mention_only and reply_gate; "
                      f"treating it as mention_only (reply_gate ignored)")

    unlisted = channels_config.get("unlisted_channels")
    if unlisted is not None and unlisted not in ("ignore", "mention_only"):
        log.error("unlisted_channels=%r is not supported (ignore, mention_only); using ignore", unlisted)

    _config_state["mtimes"] = _config_mtimes()
    log.info(f"Loaded config for {len(agent_config)} agents, {len(channels_config.get('channels', {}))} channels")


# Config freshness: a shard added by the server's /reload must take effect here
# without restarting the relay.
CONFIG_RECHECK_S = 5
_config_state = {"mtimes": None, "checked": 0.0, "failed": None}


def _config_mtimes():
    out = []
    for p in (AGENTS_CONFIG_PATH, CHANNELS_CONFIG_PATH):
        try:
            out.append(p.stat().st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


def maybe_reload_config():
    """Re-run load_config when agents.yaml or channels.json changed (checked at
    most every CONFIG_RECHECK_S). A failed load keeps the previous config and
    logs once per file version."""
    global agent_config, channels_config, registry_obj
    st = _config_state
    if st["mtimes"] is None:
        return  # never loaded here; nothing to compare against
    now = time.monotonic()
    if now - st["checked"] < CONFIG_RECHECK_S:
        return
    st["checked"] = now
    current = _config_mtimes()
    if current == st["mtimes"]:
        return
    saved = (agent_config, channels_config, registry_obj, dict(discord_id_to_agent),
             dict(discord_role_id_to_agent))
    try:
        load_config()
        if registry_obj is None:
            raise RuntimeError("agent registry unusable")
        st["failed"] = None
    except Exception as e:
        agent_config, channels_config, registry_obj = saved[:3]
        discord_id_to_agent.clear()
        discord_id_to_agent.update(saved[3])
        discord_role_id_to_agent.clear()
        discord_role_id_to_agent.update(saved[4])
        if st["failed"] != current:
            log.error(f"config reload failed, keeping previous config: {e}")
        st["failed"] = current


_ux_cache = {"src": None, "cfg": None}


def ux_config() -> "discord_ux.UxConfig":
    """Parsed channels.json "ux" blocks; re-parsed when the config is reloaded."""
    if _ux_cache["src"] is not channels_config or _ux_cache["cfg"] is None:
        cfg, warnings = discord_ux.parse_ux(channels_config)
        for w in warnings:
            log.warning(f"channels.json ux: {w}")
        _ux_cache["src"], _ux_cache["cfg"] = channels_config, cfg
    return _ux_cache["cfg"]


def sys_command_default(channel_name) -> Optional[str]:
    """The agent a system command targets by default in a channel. Routing
    (lib/routing.py) does not use this; commands run in channels it refuses."""
    return default_agent_for(channel_name)


def default_agent_for(channel_name) -> Optional[str]:
    """The agent that owns a channel's default routing: the registry shard
    listing the channel (replaces channels.json `default_agent`)."""
    if not channel_name or registry_obj is None:
        return None
    shard = registry_obj.shard_for_channel(channel_name)
    return shard.agent if shard else None


def unlisted_mentions_enabled() -> bool:
    return (channels_config or {}).get("unlisted_channels") == "mention_only"


def load_server_ids(config: Dict) -> set:
    """Discord server IDs this relay will accept messages from.

    `server_id` (a single string, what setup.sh writes) stays supported. A
    system that also needs to reach a shared server — a second household, a
    server where agents from different installs talk to each other — adds
    `server_ids` alongside it, and both are honoured:

        {"server_id": "111", "server_ids": ["222", "333"], "channels": {...}}

    Channels are still matched by ID, so a channel only routes if it's listed
    in `channels` regardless of which server it lives in.
    """
    ids = set()
    single = config.get("server_id")
    if single:
        ids.add(str(single))
    extra = config.get("server_ids") or []
    if isinstance(extra, (str, int)):
        extra = [extra]
    ids.update(str(s) for s in extra if s)
    return ids

# =============================================================================
# Bot presence (busy/idle)
# =============================================================================

PRESENCE_POLL_SECONDS = float(os.environ.get("PRESENCE_POLL_SECONDS", "5"))
# Discord rate-limits presence updates (about 5 per 20s per session). A turn
# that flaps busy/idle faster than this is collapsed rather than mirrored.
PRESENCE_MIN_INTERVAL = float(os.environ.get("PRESENCE_MIN_INTERVAL", "15"))
# A turn that ends must stay ended this long before presence goes idle, so
# back-to-back queued turns do not flicker.
PRESENCE_IDLE_HOLD = float(os.environ.get("PRESENCE_IDLE_HOLD", "10"))
PRESENCE_ENABLED = os.environ.get("PRESENCE_ENABLED", "true").lower() not in ("0", "false", "no")

BUSY_STATES = {"PROCESSING"}


def aiohttp_timeout(seconds):
    import aiohttp
    return aiohttp.ClientTimeout(total=seconds)


def agents_busy(health: dict) -> bool:
    """True when any agent in an agent-server /health payload is mid-turn."""
    agents = (health or {}).get("agents") or {}
    return any(
        (info or {}).get("state") in BUSY_STATES for info in agents.values()
    )


class PresenceTracker:
    """Decide when the bot's Discord presence should change.

    Pure state machine (clock injected) so the debounce is testable. Feed it
    observations with observe(); it returns the target ("busy"/"idle") when a
    presence update should be sent now, else None.

    - Busy applies as soon as the rate limit allows.
    - Idle applies only after the agent has been continuously idle for
      idle_hold seconds.
    - No update is sent within min_interval of the previous one.
    - reset() forgets what Discord holds (reconnect/session loss), so the
      current state is re-sent on the next observation.
    """

    def __init__(self, min_interval=PRESENCE_MIN_INTERVAL,
                 idle_hold=PRESENCE_IDLE_HOLD, clock=time.monotonic):
        self.min_interval = min_interval
        self.idle_hold = idle_hold
        self.clock = clock
        self.applied = None        # what Discord currently shows
        self.last_sent = None      # monotonic time of last update
        self.idle_since = None

    def reset(self):
        self.applied = None
        self.last_sent = None  # a fresh session has no rate-limit history

    def observe(self, busy: bool):
        now = self.clock()
        if busy:
            self.idle_since = None
            target = "busy"
        else:
            if self.idle_since is None:
                self.idle_since = now
            if self.applied is not None and now - self.idle_since < self.idle_hold:
                return None
            target = "idle"
        if target == self.applied:
            return None
        if self.last_sent is not None and now - self.last_sent < self.min_interval:
            return None
        self.applied = target
        self.last_sent = now
        return target


def presence_for(target: str):
    """(discord.Status, discord.Activity|None) for a tracker target."""
    if target == "busy":
        return discord.Status.dnd, discord.Activity(
            type=discord.ActivityType.playing, name="working on a turn")
    return discord.Status.online, None


# =============================================================================
# Discord Adapter
# =============================================================================

class DiscordAdapter(discord.Client):
    """Discord message routing to agent server"""

    def __init__(self, *args, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True
        super().__init__(intents=intents, *args, **kwargs)

        self.http_session = None
        self.server_ids = set()
        self.reply_gate = ReplyGate()
        self.channel_context = channel_context.ChannelContext()
        self.guest_budget = GuestBudget()
        self.presence = PresenceTracker()
        self._presence_task = None
        self._health_task = None

    async def setup_hook(self):
        """Initialize HTTP session"""
        import aiohttp
        self.http_session = aiohttp.ClientSession()
        self.server_ids = load_server_ids(channels_config)
        log.info(
            "Discord adapter initialized (servers: %s)",
            ", ".join(sorted(self.server_ids)) or "none configured",
        )

    async def on_ready(self):
        """Bot logged in"""
        log.info(f"Discord bot ready as {self.user.name} (ID: {self.user.id})")
        await self.write_health_heartbeat()
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self.health_loop())
        self.start_presence_loop()

    async def on_resumed(self):
        """Gateway session resumed — presence may not have survived."""
        self.presence.reset()

    async def on_connect(self):
        """New gateway connection — Discord holds no presence for it yet."""
        self.presence.reset()

    def start_presence_loop(self):
        if not PRESENCE_ENABLED:
            return
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(self.presence_loop())

    async def fetch_agents_busy(self) -> Optional[bool]:
        """Ask the agent server whether any turn is in flight; None if unknown."""
        try:
            async with self.http_session.get(
                f"{AGENT_SERVER_URL}/health",
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"},
                timeout=aiohttp_timeout(5),
            ) as resp:
                if resp.status != 200:
                    return None
                return agents_busy(await resp.json())
        except Exception:
            return None

    async def presence_tick(self):
        """One poll: update presence if the tracker says it is due."""
        busy = await self.fetch_agents_busy()
        if busy is None:
            return  # agent server unreachable: keep what is shown
        target = self.presence.observe(busy)
        if target is None or self.is_closed():
            return
        status, activity = presence_for(target)
        try:
            await self.change_presence(status=status, activity=activity)
        except Exception as e:
            log.warning("change_presence failed: %s", e)
            self.presence.reset()  # retry on the next tick

    async def presence_loop(self):
        while not self.is_closed():
            try:
                await self.presence_tick()
            except Exception as e:
                log.warning("presence tick error: %s", e)
            await asyncio.sleep(PRESENCE_POLL_SECONDS)

    async def on_message(self, message: discord.Message):
        """Route Discord message to agent"""
        # Our own posts feed the reply gate's participation rule before they are
        # discarded — a channel we are actively speaking in is one we should
        # keep answering in, and that is only knowable from our own traffic.
        if message.author == self.user:
            self.channel_context.note(message.channel.id, message.id, message.author.display_name,
                                      message.content, own=True)
            self.reply_gate.note_agent_post(message.channel.id)
            own_name, _ = self.resolve_channel(message.channel)
            if own_name and ((channels_config.get("channels", {}).get(own_name) or {})
                             .get("reply_gate")):
                self.reply_gate.note_message(
                    message.channel.id, message.author.display_name,
                    message.content, is_agent=True)
            return

        # Ignore messages from servers we aren't configured for
        if message.guild and str(message.guild.id) not in self.server_ids:
            return

        self.channel_context.note(message.channel.id, message.id, message.author.display_name,
                                  message.content)

        # Capture message
        await self.capture_message(message)

        maybe_reload_config()

        # A thread is not listed in channels.json; it resolves to its parent.
        own_name = self.get_channel_name(str(message.channel.id))
        channel_name, thread_parent = self.resolve_channel(message.channel)
        parent_name = channel_name if own_name is None else None
        channel_config = {}
        if channel_name:
            channel_config = channels_config.get("channels", {}).get(channel_name, {}) or {}

        # The mentioned agent (if any); routing is decided below.
        mention_only = channel_config.get("mention_only") is True
        target_agent = None
        for mention in message.mentions:
            if mention.bot and mention.id in discord_id_to_agent:
                target_agent = discord_id_to_agent[mention.id]
                break
        if target_agent is None:
            for role in getattr(message, "role_mentions", None) or ():
                agent = discord_role_id_to_agent.get(role.id)
                if agent:
                    target_agent = agent
                    break

        # System commands run here, not in the agent. An agent that has stopped
        # reading its queue cannot process its own `/clear`, and that is the
        # case the command exists for — so this intercept sits ahead of routing
        # and never reaches send_to_agent_server. It also sits ahead of both
        # gates below: an owner typing `/clear` in a gated channel is the least
        # ambiguous message there is, and a wedged agent is exactly when you
        # cannot afford a gate to swallow it.
        parsed = parse_sys_command(message.content)
        if parsed:
            await self.handle_sys_command(message, parsed[0], parsed[1],
                                          target_agent, sys_command_default(channel_name))
            return

        agent_ids = set(discord_id_to_agent.keys())
        if self.user:
            agent_ids.add(self.user.id)

        if message.author.bot:
            # A sibling agent's post also counts as us participating.
            if message.author.id in discord_id_to_agent:
                self.reply_gate.note_agent_post(message.channel.id)

            if not await self.allow_bot_message(message, target_agent, channel_config):
                return
        else:
            # A human spoke: the bot-to-bot budget refills.
            self.guest_budget.reset(message.channel.id)

            raw_gate = channel_config.get("reply_gate")
            if raw_gate and not mention_only:
                gate_cfg = reply_gate_config.parse(raw_gate)
                replied_to = None
                ref = getattr(message, "reference", None)
                resolved = getattr(ref, "resolved", None) if ref else None
                if resolved is not None and getattr(resolved, "author", None) is not None:
                    replied_to = resolved.author.id

                verdict, reason = self.reply_gate.decide(
                    channel_id=message.channel.id,
                    content=message.content,
                    mentions_agent=target_agent is not None,
                    replied_to_author_id=replied_to,
                    agent_ids=agent_ids,
                    agent_names=list(agent_config.keys()),
                )
                if verdict == "ask" and gate_cfg.classifier and (message.content or "").strip():
                    verdict, reason = await self.reply_gate.tier2(
                        channel_id=message.channel.id,
                        cfg=gate_cfg,
                        agent_names=list(agent_config.keys()),
                        author_name=message.author.display_name,
                        content=message.content,
                        db_path=WORKSPACE_ROOT / "data" / "memory" / "agent-server.db",
                        on_cost=lambda cost: self.post_classifier_cost(
                            channel_name, channel_config, cost),
                    )
                    await self.write_health_heartbeat()
                self.reply_gate.note_message(
                    message.channel.id, message.author.display_name, message.content)
                if verdict != "engage":
                    log.info(
                        f"[gate] #{channel_name or message.channel.id} "
                        f"{message.author.display_name}: {verdict} ({reason})"
                    )
                    return

        # B24: an unlisted channel never routes, unless channels.json sets
        # "unlisted_channels": "mention_only", in which case an @mention does.
        unlisted_mentions = channel_name is None and unlisted_mentions_enabled()
        if channel_name is None and not (unlisted_mentions and target_agent):
            if target_agent:
                log.info(f"route channel={message.channel.id} unlisted, mention ignored")
            return

        route = routing.route_message(
            registry_obj, own_name, target_agent, bool(message.author.bot),
            channel_opt_out=channel_config.get("route") is False,
            parent_channel_name=parent_name,
            mention_only=mention_only,
            unlisted_mentions=unlisted_mentions,
        ) if registry_obj is not None else None
        if route is None:
            return  # No routing

        log.info(f"route channel={channel_name} -> {route.shard} ({route.reason})")
        await self.send_to_agent_server(message, route)

    async def allow_bot_message(self, message: discord.Message,
                                target_agent: Optional[str],
                                channel_config: Dict) -> bool:
        """Whether a message from another bot may be routed to an agent.

        Two rules, and the first is the one that matters. A bot NEVER routes on
        a channel's default agent — it must @mention an agent by name. Two
        installs sharing a channel with a default agent answer each other
        forever otherwise, and the bill is the first anyone hears about it.

        Bots outside our own agent registry additionally need the channel to
        opt in with `"guest_agents": true`; a stranger's bot in a shared server
        is not something an install should have to notice to be safe from.
        """
        channel_name = self.get_channel_name(str(message.channel.id)) or message.channel.id
        known_sibling = message.author.id in discord_id_to_agent

        if not known_sibling and not channel_config.get("guest_agents"):
            return False

        if not target_agent:
            return False  # bots must address an agent explicitly

        allowed, turns, announce = self.guest_budget.take(message.channel.id)
        if allowed:
            log.info(
                f"[guest {turns}/{self.guest_budget.limit}] #{channel_name} "
                f"{message.author.display_name} -> {target_agent}"
            )
            return True

        log.warning(
            f"[guest] #{channel_name} {message.author.display_name} -> {target_agent}: "
            f"turn {turns} exceeds GUEST_TURN_LIMIT ({self.guest_budget.limit}), "
            f"staying quiet until a human speaks"
        )
        if announce:
            try:
                await message.channel.send(
                    f"`[SYS]` Stopping here — {self.guest_budget.limit} bot-to-bot turns "
                    f"with no human in between (GUEST_TURN_LIMIT). "
                    f"I'll pick back up when a person says something."
                )
            except Exception as e:
                log.error(f"Failed to post guest-limit notice: {e}")
        return False

    async def on_interaction(self, interaction: discord.Interaction):
        """Dispatch a registered Discord application ("slash") command.

        This is the entry point for every `/` command in the picker. It
        delegates to the same `handle_sys_command` the text intercept uses,
        so the two surfaces cannot drift apart, and command *definitions*
        live in bin/register-discord-commands.py rather than here:
        discord.py's CommandTree wants a Bot/tree it owns, and this relay
        runs a bare `discord.Client`. Registering over the REST API keeps
        that arrangement at the cost of one script run per definition change
        (entrypoint.sh already makes that run on every container start).
        """
        data = getattr(interaction, "data", None) or {}
        name = data.get("name", "")

        # Logged before any filter. A command that reaches the gateway and is
        # dropped here looks identical, from Discord's side, to one that never
        # arrived — and that ambiguity is unresolvable after the fact.
        log.info(
            "[interaction] type=%s name=%r user=%s",
            getattr(interaction, "type", None), name,
            getattr(getattr(interaction, "user", None), "id", None),
        )

        if interaction.type is discord.InteractionType.component:
            await self.on_component_interaction(interaction)
            return
        if getattr(interaction, "type", None) != discord.InteractionType.application_command:
            return
        if name not in SLASH_COMMANDS:
            log.warning("[interaction] dropped: %r is not a known slash command", name)
            return

        # Every reply this handler produces goes to a channel. Without a
        # channel there is nowhere to say "permission denied" or "done", and
        # the command would fail with a traceback in the log and total silence
        # in Discord — the exact symptom #86 was filed over.
        if getattr(interaction, "channel", None) is None:
            log.warning("[interaction] dropped /%s: no channel on the interaction", name)
            return

        options = {o["name"]: o.get("value") for o in (data.get("options") or [])}
        args = slash_args(name, options)

        # Acknowledge inside Discord's 3s deadline. /clear and /interrupt kill
        # and respawn a subprocess and blow straight through it; without an
        # early ack the user sees "the application did not respond" while the
        # command is in fact working. The real output still lands in-channel
        # via sys_reply, same as the text path.
        try:
            await interaction.response.send_message(
                f"`[SYS]` running `/{name} {args}`".strip(), ephemeral=True
            )
        except Exception as e:
            log.warning("[interaction] ack failed for /%s: %s", name, e)

        channel_name = self.get_channel_name(str(getattr(interaction.channel, "id", "")))
        channel_default = None
        if channel_name:
            channel_default = default_agent_for(channel_name)

        # handle_sys_command reads .channel, .author and .content off a
        # message. An interaction carries all three under different names;
        # shim it rather than fork the handler.
        shim = SimpleNamespace(
            id=getattr(interaction, "id", 0),
            channel=interaction.channel,
            author=interaction.user,
            guild=getattr(interaction, "guild", None),
            content=f"/{name} {args}".strip(),
        )
        mentioned = (options.get("agent") or "").strip() or None
        await self.handle_sys_command(shim, name, args, mentioned, channel_default)

    async def sys_reply(self, message: discord.Message, text: str):
        """Answer a system command in the channel it was typed in."""
        try:
            for chunk in split_discord_message(f"`[SYS]` {text}"):
                await message.channel.send(chunk)
        except Exception as e:
            log.error(f"Failed to post system command reply: {e}")

    async def handle_sys_command(self, message: discord.Message, cmd: str,
                                 args: str, mentioned: Optional[str],
                                 channel_default: Optional[str]):
        """Run a relay-side system command. Owner only."""
        author = f"{message.author.display_name} ({message.author.id})"

        # Logged before the permission check, not after: a denied attempt is
        # the one you most want in the log.
        log.info(f"[SYS] {author} -> /{cmd} {args}".strip())

        # An unset OWNER_DISCORD_ID denies everyone. The alternative — treating
        # "not configured" as "no restriction" — hands session control to any
        # member of the server on a fresh install that skipped the setting.
        if not OWNER_DISCORD_ID:
            await self.sys_reply(message, "Permission denied — OWNER_DISCORD_ID is not configured.")
            log.warning(f"[SYS] denied /{cmd} from {author}: OWNER_DISCORD_ID unset")
            return

        if message.author.id != OWNER_DISCORD_ID:
            await self.sys_reply(message, "Permission denied.")
            log.warning(f"[SYS] denied /{cmd} from {author}")
            return

        # Whole-install questions run ahead of target resolution rather than
        # demanding a target: headroom, health and log tails are not per-agent
        # facts, and asking "which agent?" about them is a dead end.
        if cmd in UNTARGETED_COMMANDS:
            if cmd == "status":
                await self.sys_status(message)
            elif cmd == "usage":
                await self.sys_usage(message)
            elif cmd == "health":
                await self.sys_health(message)
            elif cmd == "logs":
                await self.sys_logs(message, args)
            elif cmd == "help":
                await self.sys_help(message)
            return

        agent, err = resolve_target_agent(mentioned, channel_default, agent_config.keys())
        if err:
            await self.sys_reply(message, err)
            return

        if cmd == "clear":
            ok, detail = await self.agent_server_post(f"/agents/{agent}/reset")
            await self.sys_reply(
                message,
                f"`{agent}` session cleared." if ok else f"clear failed for `{agent}` — {detail}")
        elif cmd == "reload":
            ok, detail = await self.agent_server_post(f"/agents/{agent}/reload")
            await self.sys_reply(
                message,
                f"`{agent}` reloaded, session preserved." if ok else f"reload failed for `{agent}` — {detail}")
        elif cmd == "interrupt" and args:
            await self.sys_interrupt_with_message(message, agent, args)
        elif cmd == "interrupt":
            ok, detail, body = await self.agent_server_post_json(f"/agents/{agent}/interrupt")
            if not ok:
                await self.sys_reply(message, f"interrupt failed for `{agent}` — {detail}")
            elif body.get("interrupted"):
                await self.sys_reply(message, f"`{agent}` interrupted, session preserved.")
            else:
                # Distinguishing these two matters: "stopped it" and "there was
                # nothing running" look identical from the channel otherwise,
                # and the second one means your interrupt arrived too late.
                await self.sys_reply(message, f"`{agent}` was not generating — nothing to interrupt.")
        elif cmd == "kill":
            ok, detail, body = await self.agent_server_post_json(f"/agents/{agent}/kill")
            if not ok:
                await self.sys_reply(message, f"kill failed for `{agent}` — {detail}")
            else:
                await self.sys_reply(
                    message,
                    f"`{agent}` subprocess killed." if body.get("was_running")
                    else f"`{agent}` had no subprocess running.")
        elif cmd == "flush":
            ok, detail, body = await self.agent_server_post_json(f"/agents/{agent}/flush")
            await self.sys_reply(
                message,
                f"`{agent}` queue flushed — {body.get('flushed', 0)} message(s) dropped."
                if ok else f"flush failed for `{agent}` — {detail}")
        elif cmd == "pause":
            await self.sys_pause(message, agent, args)
        elif cmd == "resume":
            ok, detail, body = await self.agent_server_post_json(f"/agents/{agent}/resume")
            await self.sys_reply(
                message,
                f"`{agent}` resumed." if ok else f"resume failed for `{agent}` — {detail}")
        elif cmd == "effort":
            await self.sys_effort(message, agent, args)
        elif cmd == "model":
            await self.sys_model(message, agent, args)
        elif cmd == "cost":
            await self.sys_cost(message, agent)

    def interrupt_shard(self, message, agent: str) -> str:
        """The shard an interrupt message is addressed to: the one that owns the
        channel and belongs to the agent, else the agent's first shard. Works in
        channels that are not in channels.json (route_message answers None)."""
        route = None
        if registry_obj is not None:
            channel_name = self.get_channel_name(str(message.channel.id))
            route = routing.route_message(registry_obj, channel_name, agent, False)
        if route is not None and route.agent == agent:
            return route.shard
        try:
            shards = registry_obj.shards_of(agent) if registry_obj is not None else []
        except Exception:
            shards = []
        return shards[0].id if shards else agent

    async def sys_interrupt_with_message(self, message, agent: str, text: str):
        """/interrupt with a message: stop the turn, run the message first (2.5)."""
        if len(text) > INTERRUPT_MESSAGE_MAX:
            await self.sys_reply(
                message, f"interrupt message is too long ({len(text)} of "
                         f"{INTERRUPT_MESSAGE_MAX} characters).")
            return
        shard = self.interrupt_shard(message, agent)
        ok, detail, body = await self.agent_server_post_json(
            f"/agents/{agent}/interrupt?shard={shard}",
            {"message": text, "channel_id": str(message.channel.id),
             "author": message.author.display_name})
        if not ok:
            await self.sys_reply(message, f"interrupt failed for `{agent}` — {detail}")
        elif body.get("interrupted"):
            await self.sys_reply(message, f"`{agent}` interrupted; your message runs next.")
        else:
            await self.sys_reply(
                message, f"`{agent}` was not generating; your message is queued.")

    async def sys_effort(self, message, agent: str, args: str):
        """Set the agent-level effort override (owner only, via handle_sys_command)."""
        level = args.strip().lower()
        if level not in EFFORT_LEVELS:
            await self.sys_reply(message, "effort level must be one of "
                                 + ", ".join(EFFORT_LEVELS) + ".")
            return
        ok, detail, body = await self.agent_server_post_json(
            f"/agents/{agent}/effort", {"level": level})
        if not ok:
            await self.sys_reply(message, f"effort failed for `{agent}` — {detail}")
            return
        applied, deferred = body.get("applied") or [], body.get("deferred") or []
        now = body.get("effort") or "the CLI default"
        parts = [f"`{agent}` effort is now {now}."]
        if applied:
            parts.append("Applied now: " + ", ".join(f"`{x}`" for x in applied) + ".")
        if deferred:
            parts.append("After the current turn: " + ", ".join(f"`{x}`" for x in deferred) + ".")
        await self.sys_reply(message, " ".join(parts))

    async def sys_model(self, message, agent: str, args: str):
        """Set the agent-level model override (owner only). The allowed ids live
        in config/models.yaml; the agent server checks them."""
        model = args.strip()
        if not model:
            await self.sys_reply(message, "usage: /model <model id|default>")
            return
        ok, detail, body = await self.agent_server_post_json(
            f"/agents/{agent}/model", {"model": model})
        if not ok:
            await self.sys_reply(message, f"model failed for `{agent}` — {detail}")
            return
        applied, deferred = body.get("applied") or [], body.get("deferred") or []
        parts = [f"`{agent}` model is now {body.get('model')} ({body.get('source')})."]
        if applied:
            parts.append("Applied now: " + ", ".join(f"`{x}`" for x in applied) + ".")
        if deferred:
            parts.append("After the current turn: " + ", ".join(f"`{x}`" for x in deferred) + ".")
        await self.sys_reply(message, " ".join(parts))

    async def sys_pause(self, message, agent: str, args: str):
        """Hold the agent's queue (owner only, via handle_sys_command)."""
        minutes = None
        if args:
            try:
                minutes = int(args)
            except ValueError:
                minutes = -1
            if not 1 <= minutes <= 1440:
                await self.sys_reply(message, "pause minutes must be 1 to 1440.")
                return
        ok, detail, body = await self.agent_server_post_json(
            f"/agents/{agent}/pause",
            {"minutes": minutes, "by": message.author.display_name})
        if not ok:
            await self.sys_reply(message, f"pause failed for `{agent}` — {detail}")
            return
        until = body.get("until")
        when = (f"until {datetime.fromtimestamp(until, tz=timezone.utc).strftime('%H:%M UTC')}"
                if until else "until resumed")
        await self.sys_reply(
            message, f"`{agent}` paused {when}; the turn in progress will finish.")

    async def sys_status(self, message: discord.Message):
        """Report each agent's state, liveness and queue depth."""
        try:
            async with self.http_session.get(
                f"{AGENT_SERVER_URL}/health",
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    await self.sys_reply(message, f"agent server returned {resp.status}: {body[:200]}")
                    return
                health = await resp.json()
        except Exception as e:
            await self.sys_reply(message, f"could not reach the agent server — {e}")
            return

        agents = health.get("agents") or {}
        if not agents:
            await self.sys_reply(message, "No agents are configured.")
            return

        lines = []
        for name in sorted(agents):
            info = agents[name] or {}
            lines.append(
                f"`{name}` — {info.get('state', 'UNKNOWN')}, "
                f"{'alive' if info.get('alive') else 'not running'}, "
                f"queue {info.get('queue_depth', 0)}"
            )
        await self.sys_reply(message, "\n".join(lines))

    async def sys_usage(self, message: discord.Message):
        """Report rate-limit headroom — the limit that actually stops a turn."""
        try:
            async with self.http_session.get(
                f"{AGENT_SERVER_URL}/usage",
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    await self.sys_reply(message, f"agent server returned {resp.status}: {body[:200]}")
                    return
                usage = await resp.json()
        except Exception as e:
            await self.sys_reply(message, f"could not reach the agent server — {e}")
            return

        agents = usage.get("agents") or {}
        if not agents:
            await self.sys_reply(message, "No agents are configured.")
            return

        lines = [f"`{name}` — {(agents[name] or {}).get('summary', 'no reading')}"
                 for name in sorted(agents)]
        await self.sys_reply(message, "\n".join(lines))

    async def sys_health(self, message: discord.Message):
        """Report the health-monitor's verdict, component by component.

        Shelling out to bin/health-monitor.py --check rather than
        reimplementing the thresholds here is the point: an operator asking
        "is it healthy" must get the same answer the scheduled alert would
        give, or one of the two is lying.
        """
        script = WORKSPACE_ROOT / "bin" / "health-monitor.py"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), "--check",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Passed explicitly, not inherited: the monitor resolves its
                # health directory from WORKSPACE_ROOT at import time, and a
                # child that disagrees with the relay about where that is
                # reports a perfectly healthy empty directory.
                env={**os.environ, "WORKSPACE_ROOT": str(WORKSPACE_ROOT)},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            await self.sys_reply(message, "health check timed out after 20s.")
            return
        except Exception as e:
            await self.sys_reply(message, f"could not run the health monitor — {e}")
            return

        if proc.returncode != 0:
            await self.sys_reply(
                message,
                f"health monitor exited {proc.returncode}: {stderr.decode(errors='replace')[:200]}")
            return

        try:
            report = json.loads(stdout.decode())
        except (ValueError, UnicodeDecodeError) as e:
            await self.sys_reply(message, f"health monitor returned unreadable output — {e}")
            return

        components = report.get("components") or {}
        lines = ["All components healthy." if report.get("healthy") else "Health check failures:"]
        for name in sorted(components):
            info = components[name] or {}
            lines.append(f"`{name}` — {'ok' if info.get('healthy') else info.get('reason') or 'unhealthy'}")
        await self.sys_reply(message, "\n".join(lines))

    async def sys_cost(self, message: discord.Message, agent: str):
        """Today's and this month's spend for one agent.

        Reads the same GET /cost/{agent} endpoint bin/cost-report.sh curls,
        so the channel and the CLI cannot report different numbers.
        """
        try:
            async with self.http_session.get(
                f"{AGENT_SERVER_URL}/cost/{agent}",
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    await self.sys_reply(message, f"agent server returned {resp.status}: {body[:200]}")
                    return
                report = await resp.json()
        except Exception as e:
            await self.sys_reply(message, f"could not reach the agent server — {e}")
            return

        await self.sys_reply(
            message,
            f"`{agent}` — today ${report.get('daily', 0.0):.2f}, "
            f"month ${report.get('monthly', 0.0):.2f}, "
            f"session ${report.get('session', 0.0):.2f}"
        )

    async def sys_logs(self, message: discord.Message, args: str):
        """Tail a service log into the channel.

        The service name becomes a path segment, so it is validated against a
        character class and then resolved and re-checked against the logs
        directory. A rejected name is answered with the names that would have
        worked — a bare "no" leaves you guessing at a filename.
        """
        parts = (args or "").split()
        service = parts[0] if parts else ""
        available = sorted(p.stem for p in (WORKSPACE_ROOT / "logs").glob("*.log")) \
            if (WORKSPACE_ROOT / "logs").is_dir() else []
        choices = ", ".join(f"`{n}`" for n in available) or "none found"

        if not service:
            await self.sys_reply(message, f"Usage: `/logs <service> [lines]`. Available: {choices}")
            return

        lines_wanted = LOG_LINES_DEFAULT
        if len(parts) > 1:
            try:
                lines_wanted = int(parts[1])
            except ValueError:
                await self.sys_reply(message, f"`{parts[1]}` is not a number of lines.")
                return
            lines_wanted = max(1, min(lines_wanted, LOG_LINES_MAX))

        if not _LOG_NAME_RE.match(service):
            await self.sys_reply(message, f"Unknown log `{service}`. Available: {choices}")
            return

        logs_dir = (WORKSPACE_ROOT / "logs").resolve()
        path = (logs_dir / f"{service}.log").resolve()
        # Belt and braces: the regex already excludes `/` and `..`, but a
        # symlink inside logs/ is not something the name can reveal.
        if path.parent != logs_dir or not path.is_file():
            await self.sys_reply(message, f"Unknown log `{service}`. Available: {choices}")
            return

        try:
            tail = path.read_text(errors="replace").splitlines()[-lines_wanted:]
        except OSError as e:
            await self.sys_reply(message, f"could not read `{service}.log` — {e}")
            return

        if not tail:
            await self.sys_reply(message, f"`{service}.log` is empty.")
            return

        body = "\n".join(tail)
        await self.sys_reply(message, f"last {len(tail)} line(s) of `{service}.log`:\n```\n{body}\n```")

    async def sys_help(self, message: discord.Message):
        """List the commands this relay actually dispatches."""
        await self.sys_reply(
            message,
            "Commands: " + ", ".join(f"`/{c}`" for c in sorted(SLASH_COMMANDS))
        )
    async def on_component_interaction(self, interaction: discord.Interaction):
        """Deliver a button click back to the agent that is waiting on it.

        Reached via on_interaction, which owns the single `on_interaction`
        name discord.py dispatches to. #86 (slash commands) and #101 (ask
        buttons) each defined that method independently; two defs in one
        class is not an error, it just means the later one wins and the
        other surface goes silently dead. They are one entry point with two
        legs, split by interaction type.

        This is the return leg of #101. The agent server posts the question
        as an embed with buttons; Discord delivers the click here, over the
        one gateway connection this process holds; we hand the chosen index
        to POST /ask/{id}/answer, which unblocks the `ask_user` MCP tool.

        Every component interaction in the guild lands in this method, so
        anything whose custom_id is not ours is dropped without a word.
        """
        if interaction.type is not discord.InteractionType.component:
            return

        data = interaction.data or {}
        parsed = ask_handler.parse_custom_id(data.get("custom_id"))
        if parsed is None:
            return
        ask_id, index = parsed

        user = interaction.user
        ok, detail, body = await self.agent_server_post_json(
            f"/ask/{ask_id}/answer",
            {
                "index": index,
                "user_id": str(getattr(user, "id", "")),
                "user_name": getattr(user, "display_name", None) or getattr(user, "name", ""),
            },
        )
        if not ok:
            log.error(f"Could not record answer for ask {ask_id}: {detail}")
            await self.ack_interaction(interaction, "Could not reach the agent server.",
                                       resolved=False)
            return

        outcome = body.get("outcome", "unknown")
        note = body.get("note") or ask_handler.resolution_note(outcome, None)
        log.info(f"ask {ask_id} click by {getattr(user, 'id', '?')} -> {outcome}")
        await self.ack_interaction(interaction, note, resolved=(outcome == "answered"))

    async def ack_interaction(self, interaction: discord.Interaction, note: str,
                              resolved: bool):
        """Close the loop in Discord itself.

        A resolved question loses its buttons and gains the answer, so the
        channel keeps a record of what was decided. Anything else is an
        ephemeral note to the clicker only — the question stays live for
        whoever it was actually for.
        """
        try:
            if not resolved:
                await interaction.response.send_message(note, ephemeral=True)
                return
            embed = None
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.add_field(name="Answer", value=note, inline=False)
                embed.colour = discord.Colour(ask_handler.ANSWERED_COLOR)
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception as e:
            log.error(f"Failed to acknowledge interaction: {e}")

    async def agent_server_post(self, path: str):
        """POST to the agent server. Returns (ok, detail-on-failure)."""
        ok, detail, _body = await self.agent_server_post_json(path)
        return ok, detail

    async def agent_server_post_json(self, path: str, payload: Dict = None):
        """POST to the agent server. Returns (ok, detail-on-failure, body).

        `payload`, when given, is sent as the JSON body -- #101's ask-answer
        route is the first caller that needs one. aiohttp sends no body for
        the None default, so the bodyless callers are unaffected.

        `body` is always a dict so callers can `.get()` unconditionally — an
        endpoint that reports *what it did* (interrupted vs already idle,
        how many messages were flushed) is useless if reading that answer
        needs a None check at every call site.
        """
        try:
            async with self.http_session.post(
                f"{AGENT_SERVER_URL}{path}",
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"},
                json=payload,
            ) as resp:
                if resp.status == 200:
                    try:
                        body = await resp.json()
                    except Exception:
                        body = None
                    return True, "", body if isinstance(body, dict) else {}
                body = await resp.text()
                return False, f"agent server returned {resp.status}: {body[:200]}", {}
        except Exception as e:
            return False, str(e), {}

    def get_channel_name(self, channel_id: str) -> Optional[str]:
        """Get channel name from ID"""
        for name, config in channels_config.get("channels", {}).items():
            if config.get("id") == channel_id:
                return name
        return None

    def resolve_channel_ids(self, channel_id, parent_id=None):
        """(effective channel name, parent id when resolved through a thread).

        A thread has its own id, which channels.json never lists, so a message
        in one is evaluated as its parent channel (6.2). The thread's own id
        still names the conversation; only the policy lookups use the parent.
        A thread under an unlisted parent resolves to (None, None): B24 holds."""
        name = self.get_channel_name(str(channel_id))
        if name is not None or not parent_id:
            return name, None
        parent_name = self.get_channel_name(str(parent_id))
        return parent_name, (str(parent_id) if parent_name else None)

    def resolve_channel(self, channel):
        return self.resolve_channel_ids(
            channel.id, getattr(channel, "parent_id", None))

    # -- Optional behaviours (6.2): reaction notices and edit reroute ---------

    async def _lookup_channel(self, channel_id):
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                return None
        return channel

    def _ux_state(self):
        st = getattr(self, "_ux_runtime", None)
        if st is None:
            st = self._ux_runtime = {"seen": {}, "rate": {}}
        return st

    async def on_raw_reaction_add(self, payload):
        """Tell an agent that a person reacted to one of its messages."""
        try:
            await self._reaction_notice(payload)
        except Exception as e:
            log.warning("reaction notice failed: %s", e)

    async def _reaction_notice(self, payload):
        if payload.guild_id and str(payload.guild_id) not in self.server_ids:
            return
        maybe_reload_config()
        if payload.user_id in discord_id_to_agent or (
                self.user is not None and payload.user_id == self.user.id):
            return
        member = getattr(payload, "member", None)
        if member is not None and getattr(member, "bot", False):
            return
        if not ux_config().any_on():
            return
        channel = await self._lookup_channel(payload.channel_id)
        if channel is None:
            return
        name, thread_parent = self.resolve_channel(channel)
        if name is None:
            return
        mode = ux_config().for_channel(name).reaction_notices
        if mode is None:
            return
        if mode == "owner":
            if not OWNER_DISCORD_ID or payload.user_id != OWNER_DISCORD_ID:
                return
        elif member is None:
            # "humans": without the member we cannot rule out a bot.
            try:
                user = self.get_user(payload.user_id) or await self.fetch_user(payload.user_id)
            except Exception:
                return
            if user is None or getattr(user, "bot", False):
                return
            member = user

        st = self._ux_state()
        now = time.monotonic()
        key = (payload.user_id, payload.message_id)
        last = st["seen"].get(key)
        if last is not None and now - last < REACTION_NOTICE_COOLDOWN_S:
            return
        recent = st["rate"].setdefault(payload.channel_id, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if len(recent) >= REACTION_NOTICE_RATE:
            return

        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception as e:
            log.info("reaction notice: could not fetch message %s: %s", payload.message_id, e)
            return
        agent = discord_id_to_agent.get(getattr(msg.author, "id", None))
        if agent is None:
            return
        route = routing.route_message(
            registry_obj, name, agent, False,
            channel_opt_out=(channels_config.get("channels", {}).get(name) or {}).get("route") is False,
            mention_only=(channels_config.get("channels", {}).get(name) or {}).get("mention_only") is True,
        ) if registry_obj is not None else None
        if route is None:
            return

        st["seen"][key] = now
        recent.append(now)
        if len(st["seen"]) > 2048:
            for k in [k for k, t in st["seen"].items() if now - t >= REACTION_NOTICE_COOLDOWN_S]:
                st["seen"].pop(k, None)

        emoji = str(payload.emoji)
        reactor = (getattr(member, "display_name", None) if member is not None else None) \
            or f"user {payload.user_id}"
        body = {
            "agent": route.agent,
            "shard": route.shard,
            "channel": name,
            "channel_id": str(payload.channel_id),
            "server": server_label(channel),
            "author": reactor,
            "author_id": str(payload.user_id),
            "is_bot": False,
            "content": discord_ux.reaction_notice_text(reactor, emoji, msg.content or ""),
            "message_id": "reaction:{}:{}:{}".format(
                msg.id, payload.user_id, hashlib.sha1(emoji.encode()).hexdigest()[:8]),
            "mentions_agent": False,
            "attachments": [],
        }
        if thread_parent:
            body["thread_parent_id"] = thread_parent
        try:
            async with self.http_session.post(
                f"{AGENT_SERVER_URL}/message", json=body,
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"},
            ) as resp:
                if resp.status != 202:
                    log.info("reaction notice not queued: HTTP %s", resp.status)
        except Exception as e:
            log.info("reaction notice send failed: %s", e)

    async def on_raw_message_edit(self, payload):
        """Reroute a person's edit of a message an agent may already have seen."""
        try:
            await self._message_edited(payload)
        except Exception as e:
            log.warning("edit reroute failed: %s", e)

    async def _message_edited(self, payload):
        data = getattr(payload, "data", None) or {}
        if "content" not in data:
            return  # a link unfurl or pin, not an edit
        if payload.guild_id and str(payload.guild_id) not in self.server_ids:
            return
        author = data.get("author") or {}
        if author.get("bot"):
            return
        maybe_reload_config()
        if not ux_config().any_on():
            return
        channel = await self._lookup_channel(payload.channel_id)
        if channel is None:
            return
        name, _ = self.resolve_channel(channel)
        if name is None or ux_config().for_channel(name).edit_reroute is None:
            return

        content = data.get("content") or ""
        author_id = author.get("id")
        msg = None
        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception as e:
            log.info("edit reroute: could not fetch %s, using raw text: %s", payload.message_id, e)
        if msg is not None:
            if getattr(msg.author, "bot", False):
                return
            author_id = msg.author.id
            content = msg.content or content
            try:
                block = await self.resolve_reply_context(msg)
            except Exception:
                block = None
            if block:
                content = f"{block}\n{content}" if content else block
        if author_id is None:
            return
        cached = getattr(payload, "cached_message", None)
        ok, detail, body = await self.agent_server_post_json("/message/edit", {
            "server": server_label(channel),
            "message_id": str(payload.message_id),
            "channel_id": str(payload.channel_id),
            "author_id": str(author_id),
            "content": content,
            "before": getattr(cached, "content", None) if cached is not None else None,
        })
        if not ok:
            log.info("edit reroute not delivered: %s", detail)
        else:
            log.info("edit of %s: %s", payload.message_id, body.get("status"))

    async def download_attachments(self, message: discord.Message) -> List[Dict]:
        """Save a message's attachments locally and describe them for the agent.

        Called from `send_to_agent_server` rather than `on_message` so that
        files on a message the gates are about to drop are never written to
        disk at all.

        An attachment that is too large, or that fails to download, still
        comes back in the list with `path: None` and a `skipped` reason. The
        agent needs to be able to say "you sent me a 40 MB video and I could
        not open it" — going quiet about the file is the bug this fixes, and a
        failed download reproduces it exactly.
        """
        attachments = getattr(message, "attachments", None) or []
        if not attachments:
            return []

        described: List[Dict] = []
        dest_dir = ATTACHMENTS_DIR / str(message.id)

        for index, attachment in enumerate(attachments[:MAX_ATTACHMENTS_PER_MESSAGE]):
            entry = {
                "filename": attachment.filename,
                "content_type": getattr(attachment, "content_type", None),
                "size": getattr(attachment, "size", None),
                "path": None,
                "skipped": None,
            }

            size = entry["size"] or 0
            if size > MAX_ATTACHMENT_BYTES:
                entry["skipped"] = f"exceeds the {MAX_ATTACHMENT_BYTES} byte download limit"
                log.warning(
                    "Attachment %s on message %s skipped: %d bytes",
                    attachment.filename, message.id, size
                )
                described.append(entry)
                continue

            path = dest_dir / safe_attachment_name(attachment.filename, index)
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                await attachment.save(path)
                entry["path"] = str(path)
            except Exception as e:
                # One bad download must not cost the message. The text still
                # goes through, and the envelope says what failed.
                entry["skipped"] = f"download failed: {e}"
                log.error(
                    "Failed to download attachment %s on message %s: %s",
                    attachment.filename, message.id, e
                )

            described.append(entry)

        dropped = len(attachments) - len(described)
        if dropped > 0:
            described.append({
                "filename": f"<{dropped} more attachment(s)>",
                "content_type": None,
                "size": None,
                "path": None,
                "skipped": f"over the {MAX_ATTACHMENTS_PER_MESSAGE} attachment per message limit",
            })

        return described

    async def resolve_reply_context(self, message: discord.Message) -> Optional[str]:
        """Return a delimited block quoting the message this one replies to.

        Uses `reference.resolved` when Discord supplied it, otherwise fetches
        the referenced message. A deleted or unfetchable target yields a short
        placeholder rather than an exception, since the reply itself must
        still reach the agent. Returns None when the message is not a reply.
        """
        ref = getattr(message, "reference", None)
        if ref is None:
            return None
        target = getattr(ref, "resolved", None)
        if target is None or getattr(target, "author", None) is None:
            # Missing, or a DeletedReferencedMessage stub (no author).
            target = None
            ref_id = getattr(ref, "message_id", None)
            if ref_id:
                try:
                    target = await message.channel.fetch_message(ref_id)
                except Exception as e:
                    log.info("Could not fetch replied-to message %s: %s", ref_id, e)
        return format_reply_context(target)

    async def send_to_agent_server(self, message: discord.Message, route):
        """Send message to agent server"""
        agent = route.agent
        channel_name, thread_parent = self.resolve_channel(message.channel)
        # the channel's own config (a thread resolves to its parent), before the label below
        chan_cfg = (channels_config.get("channels", {}).get(channel_name) or {}) if channel_name else {}
        parent = getattr(message.channel, "parent", None)
        if not channel_name:
            channel_name = (getattr(message.channel, "name", None)
                            or getattr(parent, "name", None) or "unknown")
        if parent is not None:
            # A thread reads as "parent/thread" so the agent can tell where it is.
            parent_label = channel_name if thread_parent else getattr(parent, "name", None)
            thread_label = getattr(message.channel, "name", None)
            if parent_label and thread_label:
                channel_name = f"{parent_label}/{thread_label}"
        guild = getattr(message, "guild", None)

        attachments = await self.download_attachments(message)

        content = message.content
        try:
            reply_block = await self.resolve_reply_context(message)
        except Exception as e:
            log.error(f"Reply context lookup failed: {e}")
            reply_block = None
        if reply_block:
            content = f"{reply_block}\n{content}" if content else reply_block
        # Messages since the agent last spoke here, so "this" and "that" have a referent.
        ctx_block = self.channel_context.block(
            message.channel.id, message.id, channel_context.context_limit(chan_cfg))
        if ctx_block:
            content = f"{ctx_block}\n{content}" if content else ctx_block

        payload = {
            "agent": agent,
            "shard": route.shard,
            "channel": channel_name,
            "channel_id": str(message.channel.id),
            "server": server_label(message),
            "guild_id": str(guild.id) if guild else None,
            "author": message.author.display_name,
            "author_id": str(message.author.id),
            "is_bot": message.author.bot,
            "content": content,
            "message_id": str(message.id),
            "mentions_agent": any(m.id in discord_id_to_agent for m in message.mentions) or any(
                r.id in discord_role_id_to_agent
                for r in getattr(message, "role_mentions", None) or ()),
            "attachments": attachments,
        }
        if thread_parent:
            payload["thread_parent_id"] = thread_parent

        try:
            async with self.http_session.post(
                f"{AGENT_SERVER_URL}/message",
                json=payload,
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status == 202:
                    log.info(f"Queued message for {route.shard} from {message.author.display_name}")
                else:
                    text = await resp.text()
                    log.error(f"Agent server error {resp.status}: {text}")
                    if resp.status == 429 or resp.status >= 500:
                        spool_deferred_message(payload, f"HTTP {resp.status}")
        except Exception as e:
            log.error(f"Error sending to agent server: {e}")
            spool_deferred_message(payload, repr(e))

    async def capture_message(self, message: discord.Message):
        """Capture message to JSONL"""
        channel_name = self.get_channel_name(str(message.channel.id))

        entry = {
            "v": 1,
            "ts": datetime.now().isoformat(),
            "channel": "discord",
            "channel_id": str(message.channel.id),
            "channel_name": channel_name or "unknown",
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "is_bot": message.author.bot,
            "content": message.content,
            "message_id": str(message.id),
            # Names only — the capture log is a record of what was said, and a
            # message whose whole payload was a file reads as blank without
            # this.
            "attachments": [a.filename for a in getattr(message, "attachments", None) or []],
        }

        # Write to daily JSONL
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_file = MESSAGES_DIR / f"messages-{date_str}.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def post_classifier_cost(self, channel_name, channel_config, cost):
        """Record a classifier call's dollars under the channel's agent id."""
        route = routing.route_message(
            registry_obj, channel_name, None, False,
            channel_opt_out=channel_config.get("route") is False,
        ) if registry_obj is not None and channel_name else None
        if route is None:
            return
        ok, detail, _ = await self.agent_server_post_json(
            "/cost", {"agent": route.agent, "cost_delta": cost})
        if not ok:
            log.debug("classifier /cost failed: %s", detail)

    async def write_health_heartbeat(self):
        """Write health heartbeat"""
        try:
            HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HEALTH_FILE, "w") as f:
                json.dump({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "healthy",
                    "reply_gate": dict(self.reply_gate.stats),
                }, f)
        except OSError as e:
            log.debug("health heartbeat failed: %s", e)

    async def health_loop(self):
        """Refresh relay.json so the monitor's 300 s staleness check sees a live relay."""
        while True:
            await self.write_health_heartbeat()
            await asyncio.sleep(HEALTH_INTERVAL_S)

    async def close(self):
        """Cleanup on shutdown"""
        if self._health_task:
            self._health_task.cancel()
        if self._presence_task:
            self._presence_task.cancel()
        if self.http_session:
            await self.http_session.close()
        await super().close()

# =============================================================================
# Dispatch Adapter
# =============================================================================

class DispatchAdapter:
    """Watch inbox directories and invoke builder/reviewer scripts"""

    def __init__(self):
        self.running = False
        self.task = None

        # Initialize semaphores
        dispatch_semaphores["builder"] = asyncio.Semaphore(MAX_CONCURRENT_BUILDERS)
        dispatch_semaphores["reviewer"] = asyncio.Semaphore(MAX_CONCURRENT_REVIEWERS)

    async def start(self):
        """Start dispatch polling loop"""
        self.running = True
        self.task = asyncio.create_task(self.poll_loop())
        log.info("Dispatch adapter started")

    async def stop(self):
        """Stop dispatch adapter"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        # Wait for active dispatches
        if active_dispatches:
            log.info(f"Waiting for {len(active_dispatches)} active dispatches to complete")
            await asyncio.gather(*active_dispatches.values(), return_exceptions=True)

    async def poll_loop(self):
        """Poll inbox directories for new briefs"""
        while self.running:
            try:
                await self.check_inboxes()
                await asyncio.sleep(DISPATCH_POLL_INTERVAL)
            except Exception as e:
                log.error(f"Dispatch poll error: {e}")
                await asyncio.sleep(DISPATCH_POLL_INTERVAL)

    async def check_inboxes(self):
        """Check inbox directories for new briefs"""
        for agent_type in ["builder", "reviewer"]:
            inbox_dir = DISPATCH_INBOX_DIR / agent_type
            if not inbox_dir.exists():
                continue

            # Find brief files
            briefs = sorted(inbox_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)

            for brief_file in briefs:
                # Check if already dispatched
                if brief_file.stem in active_dispatches:
                    continue

                # Try to acquire semaphore (non-blocking)
                semaphore = dispatch_semaphores.get(agent_type)
                if semaphore and semaphore._value > 0:
                    # Dispatch
                    task = asyncio.create_task(self.dispatch(agent_type, brief_file))
                    active_dispatches[brief_file.stem] = task
                    log.info(f"Dispatched {agent_type}: {brief_file.name}")

    async def dispatch(self, agent_type: str, brief_file: Path):
        """Dispatch brief to agent"""
        semaphore = dispatch_semaphores.get(agent_type)
        if not semaphore:
            return

        async with semaphore:
            try:
                # Read brief
                with open(brief_file) as f:
                    brief_content = f.read()

                # Parse frontmatter
                metadata = self.parse_frontmatter(brief_content)
                requester = metadata.get("requester", "unknown")
                callback_channel = metadata.get("callback_channel", "general")

                # Determine invoke script
                invoke_script = WORKSPACE_ROOT / "bin" / f"invoke-{agent_type}.sh"
                if not invoke_script.exists():
                    log.error(f"Invoke script not found: {invoke_script}")
                    return

                # Invoke script
                timeout = DISPATCH_TIMEOUTS.get(agent_type, 21600)
                log.info(f"Invoking {agent_type} for {brief_file.name} (timeout: {timeout}s)")

                proc = await asyncio.create_subprocess_exec(
                    str(invoke_script),
                    str(brief_file),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                    returncode = proc.returncode

                    if returncode == 0:
                        log.info(f"{agent_type} completed: {brief_file.name}")
                    else:
                        log.error(f"{agent_type} failed with code {returncode}: {brief_file.name}")
                        log.error(f"stderr: {stderr.decode()}")

                except asyncio.TimeoutError:
                    log.error(f"{agent_type} timed out: {brief_file.name}")
                    proc.kill()
                    await proc.wait()

                # Archive brief
                archive_dir = brief_file.parent / "archive"
                archive_dir.mkdir(exist_ok=True)
                brief_file.rename(archive_dir / brief_file.name)

            finally:
                # Remove from active dispatches
                active_dispatches.pop(brief_file.stem, None)

    def parse_frontmatter(self, content: str) -> Dict:
        """Parse YAML frontmatter from brief"""
        if not content.startswith("---"):
            return {}

        lines = content.split("\n")
        frontmatter_lines = []
        in_frontmatter = False

        for i, line in enumerate(lines):
            if i == 0 and line.strip() == "---":
                in_frontmatter = True
                continue
            if in_frontmatter:
                if line.strip() == "---":
                    break
                frontmatter_lines.append(line)

        # Simple key: value parser (not full YAML)
        metadata = {}
        for line in frontmatter_lines:
            if ":" in line:
                key, _, value = line.partition(":")
                metadata[key.strip()] = value.strip()

        return metadata

# =============================================================================
# Main
# =============================================================================

async def main():
    """Main relay service"""
    log.info("Karakos relay starting")

    # Load config
    load_config()

    # Start the build queue dispatcher when enabled, else the inbox dispatch adapter
    # (never both: they would consume the same directories).
    build_queue_config = build_hosts.load_config(WORKSPACE_ROOT / "config" / "build-queue.yaml")
    dispatch = None
    if build_queue_config.invalid and build_queue_config.requested:
        log.error("build queue config invalid (%s); no dispatcher started", build_queue_config.invalid)
    elif build_queue_config.enabled:
        dispatch = build_dispatcher.build_dispatcher(WORKSPACE_ROOT, build_queue_config)
        dispatch.open()
        await dispatch.start()
    else:
        if build_queue_config.invalid:
            log.warning("build queue config invalid (%s); using the inbox dispatch adapter",
                        build_queue_config.invalid)
        dispatch = DispatchAdapter()
        await dispatch.start()

    # Get primary agent's Discord token
    primary_agent = None
    for agent_name, config in agent_config.items():
        token_env = config.get("discord_bot_token_env")
        if token_env and os.environ.get(token_env):
            primary_agent = agent_name
            break

    if not primary_agent:
        log.warning("No Discord tokens configured, Discord adapter disabled")
        # Run dispatch-only mode
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            pass
        finally:
            if dispatch:
                await dispatch.stop()
        return

    # Start Discord bot
    token = os.environ.get(agent_config[primary_agent]["discord_bot_token_env"])
    discord_client = DiscordAdapter()

    try:
        # Run Discord bot (blocks until closed)
        await discord_client.start(token)
    except KeyboardInterrupt:
        log.info("Shutdown signal received")
    finally:
        await discord_client.close()
        if dispatch:
            await dispatch.stop()
        log.info("Relay shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
