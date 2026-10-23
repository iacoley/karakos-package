#!/usr/bin/env python3
"""
Karakos Agent Server — Persistent Subprocess Architecture

Accepts messages via HTTP, queues to SQLite, sends to persistent claude
subprocess via stdin (stream-json), posts responses to Discord.

Port: 18791 (configurable via AGENT_SERVER_PORT env var)
"""

import asyncio
import errno
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from logging.handlers import RotatingFileHandler

import aiohttp
import aiosqlite
from aiohttp import web

# bin/ is a directory of scripts, not a package. Put it on the path
# explicitly so sibling modules resolve both when this file is executed
# directly and when a test loads it by path under a synthetic module name.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# lib/ holds modules shared by more than one script; it sits beside bin/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
# lib/ is a package root for lib.migrate (the schema-stamp guard).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ask_handler  # noqa: E402
import discord_ux  # noqa: E402
import hive as hive_lib  # noqa: E402
import msgqueue  # noqa: E402
import outbox as outbox_lib  # noqa: E402
import post_guard  # noqa: E402
import registry as agent_registry  # noqa: E402
import procreap  # noqa: E402
import prompt_compose  # noqa: E402
import rate_limits  # noqa: E402
import session_policy as sp_lib  # noqa: E402
import shards as shards_lib  # noqa: E402
import spawn_env as spawn_env_lib  # noqa: E402
import tengwar  # noqa: E402
import turn_loop  # noqa: E402
import usage_gate  # noqa: E402
import operator_pause  # noqa: E402
import runtime_overrides  # noqa: E402
import redact  # noqa: E402
from lib.migrate.guard import require_stamp  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
PORT = int(os.environ.get("AGENT_SERVER_PORT", "18791"))
DB_PATH = WORKSPACE_ROOT / "data" / "memory" / "agent-server.db"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.yaml"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
CLAUDE_SETTINGS_PATH = WORKSPACE_ROOT / "config" / "claude-settings.json"
STREAM_LOG_DIR = WORKSPACE_ROOT / "logs" / "agent-streams"
OUTBOX_PATH = WORKSPACE_ROOT / "data" / "outbox" / "outbox.db"

# Raw stream-json events are teed to logs/agent-streams/{agent}_*.jsonl as
# they are read (#148). The manual summarizer reads the tail of the newest
# matching file; nothing wrote these files before, so that whole path was dead. A new file is opened per boot,
# per day, and whenever the current one passes the size cap — which keeps
# each file small enough to tail cheaply and gives bin/purge-data.py whole
# files to expire rather than lines to rewrite.
STREAM_LOG_MAX_BYTES = int(os.environ.get("STREAM_LOG_MAX_BYTES", str(16 * 1024 * 1024)))
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
OWNER_DISCORD_ID = os.environ.get("OWNER_DISCORD_ID", "0")

# Attempts per chunk on the direct (non-durable) path: tool lines, notices, and
# the fallback when the outbox store is unusable. Durable replies use the
# outbox retry policy (lib/outbox.py) instead.
POST_MAX_ATTEMPTS = int(os.environ.get("DISCORD_POST_MAX_ATTEMPTS", "3"))
POST_RETRY_BASE_SEC = float(os.environ.get("DISCORD_POST_RETRY_BASE_SEC", "1.0"))

# Cost limits
COST_DAILY_LIMIT = float(os.environ.get("COST_DAILY_LIMIT", "25.00"))
COST_MONTHLY_LIMIT = float(os.environ.get("COST_MONTHLY_LIMIT", "500.00"))
COST_WARNING_THRESHOLD = float(os.environ.get("COST_WARNING_THRESHOLD", "0.75"))

# Where the rate-limit headroom warning goes. Unset means log-only: an alert
# with nowhere to go must not become an exception on the turn that raised it.
RATE_LIMIT_ALERT_CHANNEL_ID = os.environ.get("RATE_LIMIT_ALERT_CHANNEL_ID", "")

# Per-agent liveness beacons, read by bin/wedge-check.py from OUTSIDE this
# process. See write_agent_beacon.
AGENT_BEACON_DIR = WORKSPACE_ROOT / "data" / "health" / "agents"

# Beacon writes are throttled: a busy turn emits events far faster than any
# watcher samples, and the beacon only has to be fresher than the wedge
# threshold to prove liveness.
BEACON_MIN_INTERVAL_SEC = 1.0

# Queue limits
QUEUE_DEPTH_LIMIT = 50
TYPING_INTERVAL = 8  # seconds

# Mid-turn tool activity lines (#91). A turn can make dozens of tool calls
# in a few seconds; posting one Discord message each would rate-limit the
# bot and bury the channel. These lines exist to answer "is it still
# working?", not to be a complete log, so the first call posts immediately
# (liveness is the whole point) and the rest are throttled and capped.
TOOL_EVENT_MIN_INTERVAL = 5    # seconds between lines within one turn
TOOL_EVENT_MAX_PER_TURN = 12   # hard ceiling per turn
TOOL_EVENT_DETAIL_CHARS = 90   # truncation for the argument summary

# Live turn events for the dashboard chat: every thinking / interstitial-
# text / tool_use block in a turn is persisted as a typed row keyed by the
# queue message_id, which /api/chat/stream relays as typed SSE events. #91
# (above) answered "is it still working?" for Discord; this is the same
# observation point reaching the dashboard chat page, which otherwise only
# ever sees the final response text at processed=2.
TURN_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS turn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# Processing states
STATUS_QUEUED = 0
STATUS_IN_PROGRESS = 1
STATUS_COMPLETE = 2
STATUS_CRASHED = 3
STATUS_SKIPPED = 4

# Columns the 20_queue migrator step adds (lib/migrate/steps/20_queue.py).
QUEUE_V2_COLUMNS = (
    "call_id", "reply_to_agent", "priority", "expires_at", "depth",
    "partial_response", "restart_count", "claimed_by", "owner_agent",
)

# Wall-clock start of this process, for /health's uptime_seconds. The
# dashboard's "Uptime" card read that field before anything served it and
# rendered a permanent 0h.
SERVER_START_TS = time.time()

# Session persistence
SUMMARY_DIR = WORKSPACE_ROOT / "logs" / "session-summaries"

# Logging
STREAM_LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("agent-server")
log.setLevel(logging.INFO)
handler = RotatingFileHandler(
    WORKSPACE_ROOT / "logs" / "agent-server.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=7
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
log.addHandler(handler)

# Also log to console
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(console)

# Regex patterns
THINKING_BLOCK_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)

# Prefix stamped onto the prompt text sent to the subprocess when every
# message in a batch is automated (is_bot=1 — system pokes, heartbeats,
# task-complete notifications from bin/poke.sh, never a human via
# bin/relay.py). system/hooks/inject-recall.py (#98) reads this same
# literal string as its skip gate, since the hook only ever sees the final
# prompt text over stdin, not the message_queue row's is_bot column.
AUTOMATED_TRAFFIC_SENTINEL = "[KARAKOS_AUTOMATED]"

# =============================================================================
# Global State
# =============================================================================

db: Optional[aiosqlite.Connection] = None
http_session: Optional[aiohttp.ClientSession] = None
agent_config: Dict[str, Dict[str, Any]] = {}
# Shards (step 2.1): one `claude` subprocess each. Every runtime dict below is
# keyed by shard id; an agent's default shard has the agent's id. Empty means
# "no registry loaded": each key of agent_config is its own default shard.
shard_specs: List[shards_lib.ShardSpec] = []
shard_owner: Dict[str, str] = {}
channels_config: Dict[str, Any] = {}
# Optional Discord behaviours (6.2): thread id -> parent channel id (in memory,
# LRU), the parsed channels.json "ux" blocks (keyed by the config object they
# came from) and the last time a thread-creation failure was warned per channel.
ux_threads = discord_ux.LruMap(256)
_ux_parsed: Dict[str, Any] = {"src": None, "cfg": None}
_ux_warned: Dict[str, float] = {}
UX_WARN_EVERY_S = 3600
agent_processes: Dict[str, asyncio.subprocess.Process] = {}
agent_locks: Dict[str, asyncio.Lock] = {}
agent_states: Dict[str, str] = {}
response_buffers: Dict[str, str] = {}
agent_last_cost: Dict[str, float] = {}
agent_sessions: Dict[str, str] = {}
stderr_reader_tasks: Dict[str, asyncio.Task] = {}
# One task per agent awaiting its subprocess's exit (#90). Fires a respawn and
# a channel notice when the process dies on its own.
respawn_watcher_tasks: Dict[str, asyncio.Task] = {}
# monotonic timestamps of recent unexpected exits, per agent, for the
# crashloop brake below.
respawn_history: Dict[str, List[float]] = {}
RESPAWN_WINDOW_SECONDS = 300
RESPAWN_MAX_IN_WINDOW = 3
# Agents whose subprocess is being ended on purpose — restart, reload,
# interrupt, shutdown, or POST /kill. Lets the respawn watcher tell an
# intentional kill from a crash. Set by kill_agent_subprocess before it
# terminates anything, cleared by start_agent_subprocess. See the comment at
# the set site: this is the secondary guard, behind the watcher-cancel.
deliberate_kills: set = set()
# Last channel each agent actually spoke in. The respawn notice has no turn of
# its own to inherit a channel from — the process died between turns — so this
# is the only record of where the user is waiting.
agent_last_channel: Dict[str, str] = {}
# Agents whose current turn was deliberately ended by /interrupt. Read (and
# cleared) by read_agent_response so the half-written reply is discarded
# instead of posted.
interrupted_agents: set = set()
typing_tasks: Dict[str, asyncio.Task] = {}
agent_todo_lists: Dict[str, List[Dict]] = {}
active_todo_messages: Dict[str, Dict] = {}

# Outstanding multiple-choice questions (#101). Keyed by ask id; created by
# POST /ask on behalf of the MCP `ask_user` tool, resolved by the relay when
# somebody clicks a button.
ask_registry = ask_handler.AskRegistry()


class _ServerView:
    """Live read-through to this module's globals. Tests load the server under
    a synthetic name that is not in sys.modules, so the turn loop's state view
    cannot be given the module object itself."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


# The turn loop (lib/turn_loop.py) reads everything above through this view at
# access time, so rebinding a global here (a test patching db or
# post_to_discord) is seen by the loop on its next call.
STATE = turn_loop.make_state(_ServerView())
# Context budget / handoff / reset policy (step 2.6); in memory, keyed by shard.
STATE.session_policy = sp_lib.SessionPolicyState()

# Who and where the agent's current turn came from. `/ask` has no channel of
# its own — the question belongs in the conversation that prompted it — and
# the authors of that conversation are the people allowed to answer it.
agent_turn_context: Dict[str, Dict[str, Any]] = {}

# Discord token mapping
AGENT_TOKENS: Dict[str, str] = {}
DISCORD_ID_TO_AGENT: Dict[int, str] = {}

# agent id -> registry role; the usage gate never gates a `monitor`.
agent_roles: Dict[str, str] = {}

# Graceful shutdown flag
shutting_down = False

# =============================================================================
# Database Schema
# =============================================================================

async def init_db():
    """Initialize database schema"""
    global db
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row

    # Message queue table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            channel TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            server TEXT DEFAULT 'discord',
            author TEXT NOT NULL,
            author_id TEXT DEFAULT '0',
            is_bot INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            message_id TEXT UNIQUE NOT NULL,
            mentions_agent INTEGER DEFAULT 0,
            attachments TEXT,
            processed INTEGER DEFAULT 0,
            response TEXT,
            discord_response_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processing_started_at TIMESTAMP,
            processed_at TIMESTAMP,
            not_before INTEGER,
            call_id TEXT,
            reply_to_agent TEXT,
            priority INTEGER DEFAULT 0,
            expires_at TEXT,
            depth INTEGER DEFAULT 0,
            partial_response TEXT,
            restart_count INTEGER DEFAULT 0,
            claimed_by TEXT,
            owner_agent TEXT
        )
    """)

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_agent
        ON message_queue(agent, processed, created_at)
    """)

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_pending
        ON message_queue(processed) WHERE processed = 0
    """)

    # Created after the columns they name. On an upgraded install the
    # columns come from the 20_queue migrator step, never from boot.
    async with db.execute("PRAGMA table_info(message_queue)") as cursor:
        queue_cols = {row[1] for row in await cursor.fetchall()}
    missing = [c for c in QUEUE_V2_COLUMNS if c not in queue_cols]
    if missing:
        await db.close()
        raise SystemExit(
            f"message_queue lacks schema-2.0 columns {missing}; run: karakos migrate")
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_claim
        ON message_queue(agent, processed, priority DESC, created_at)
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_call ON message_queue(call_id)"
    )

    # Sessions table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            agent TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            compaction_count INTEGER DEFAULT 0,
            last_compacted TIMESTAMP,
            context_tokens INTEGER DEFAULT 0,
            context_updated_at TIMESTAMP
        )
    """)
    # `started`: the CLI has begun this session id (it emitted its init event).
    # A respawn must --resume a started session; --session-id is only valid for
    # an id the CLI has never seen ("Session ID ... is already in use").
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        had_started = "started" in {row[1] for row in await cursor.fetchall()}
    await ensure_column("sessions", "started", "INTEGER DEFAULT 0")
    if not had_started:
        # Sessions from before the column that already did work were started.
        await db.execute(
            "UPDATE sessions SET started = 1 WHERE input_tokens > 0 "
            "OR context_tokens > 0 OR compaction_count > 0")

    # Cost events table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            cost_delta REAL,
            session_total REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            duration_ms REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Rate-limit state table. Account-level: one row per window type
    # (`five_hour`, `seven_day`, ...), never per agent or shard (spec 2.7). A
    # current-headroom reading, not a history; the CLI resends it. Upgraded
    # installs are re-keyed by the 35_rate_limit migrator step, never by boot.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_state (
            rate_limit_type TEXT PRIMARY KEY,
            status TEXT,
            resets_at INTEGER,
            overage_status TEXT,
            is_using_overage INTEGER DEFAULT 0,
            utilization REAL,
            alerted_for_resets_at INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    async with db.execute("PRAGMA table_info(rate_limit_state)") as cursor:
        rl_pk = [row[1] for row in await cursor.fetchall() if row[5]]
    if rl_pk != ["rate_limit_type"]:
        await db.close()
        raise SystemExit(
            "rate_limit_state is still keyed by agent; run: karakos migrate")

    await db.execute(TURN_EVENTS_DDL)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_turn_events_msg ON turn_events(message_id, seq)"
    )

    # Migrations for databases created before a column existed. CREATE TABLE
    # IF NOT EXISTS is a no-op against an existing table, so a new column in
    # the definition above reaches upgraded installs only through here.
    await ensure_column("message_queue", "attachments", "TEXT")
    # Epoch seconds before which a QUEUED row must not be dispatched: a turn
    # that hit the usage wall is held here rather than consumed.
    await ensure_column("message_queue", "not_before", "INTEGER")
    # Conversation metrics: which session (== one context window; a cleared
    # or respawned session is a new conversation) a cost row belongs to, so
    # spend/tokens can be rolled up per conversation rather than only per
    # agent. NULL on rows written before this column existed.
    await ensure_column("cost_events", "session_id", "TEXT")

    await db.commit()
    log.info("Database initialized")


async def ensure_column(table: str, column: str, decl: str) -> None:
    """Add `column` to `table` if it is not already there.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and a second ALTER raises rather
    than passing, so the PRAGMA read is the guard.
    """
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    if column in existing:
        return
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    log.info(f"Migrated {table}: added column {column}")

# =============================================================================
# Configuration Loading
# =============================================================================

async def load_config():
    """Load agent and channel configuration from JSON files"""
    global agent_config, channels_config, AGENT_TOKENS, DISCORD_ID_TO_AGENT

    # Load agents config (config/agents.yaml via the registry). A missing or
    # invalid registry is an error: there is no legacy-config fallback, since the
    # migrator owns the 1.x conversion.
    try:
        reg = agent_registry.load_registry(WORKSPACE_ROOT)
    except agent_registry.RegistryError as e:
        # Keep whatever is already loaded (empty on first boot); never wipe a
        # running config because of a bad edit.
        log.error(f"Agent registry unusable: {e}")
    else:
        for w in reg.warnings:
            log.warning(f"registry: {w}")
        agent_config = reg.legacy_view()["agents"]
        agent_roles.clear()
        agent_roles.update({a.id: a.role for a in reg.agents()})
        _set_shard_specs(shards_lib.plan_shards(reg))
        log.info(f"Loaded configuration for {len(agent_config)} agents "
                 f"({len(shard_specs)} shards)")

    # Load channels config
    if CHANNELS_CONFIG_PATH.exists():
        with open(CHANNELS_CONFIG_PATH) as f:
            channels_config = json.load(f)
            log.info(f"Loaded {len(channels_config.get('channels', {}))} channel mappings")
    else:
        log.warning(f"Channels config not found: {CHANNELS_CONFIG_PATH}")
        channels_config = {}

    _rebuild_token_maps()


def reload_channels_config():
    """Re-read channels.json (the 6.2 "ux" switches live there). A missing or
    unreadable file keeps the running config."""
    global channels_config
    if not CHANNELS_CONFIG_PATH.exists():
        return
    try:
        with open(CHANNELS_CONFIG_PATH) as f:
            channels_config = json.load(f)
    except Exception as e:
        log.error(f"channels.json unreadable on reload, keeping previous: {e}")


def _set_shard_specs(specs):
    global shard_specs, shard_owner
    shard_specs = list(specs)
    shard_owner = {s.id: s.agent for s in shard_specs}


def effective_specs() -> List[shards_lib.ShardSpec]:
    """shard_specs, or one default shard per agent_config key when no registry
    was loaded (tests that set agent_config directly)."""
    return shard_specs or [shards_lib.ShardSpec(a, a, (), True) for a in agent_config]


def spec_of(shard: str) -> shards_lib.ShardSpec:
    for sp in effective_specs():
        if sp.id == shard:
            return sp
    return shards_lib.ShardSpec(shard, shard, (), True)


def label_of(shard: str) -> str:
    return shards_lib.shard_label(spec_of(shard))


def _rebuild_token_maps():
    """Rebuild AGENT_TOKENS / DISCORD_ID_TO_AGENT from agent_config. The agent's
    token is also registered under each of its shard ids, so post_to_discord
    and start_typing take a shard id unchanged."""
    AGENT_TOKENS.clear()
    DISCORD_ID_TO_AGENT.clear()
    for agent_name, config in agent_config.items():
        token_env_var = config.get("discord_bot_token_env")
        if token_env_var:
            token = os.environ.get(token_env_var, "")
            if token:
                AGENT_TOKENS[agent_name] = token
                for sp in effective_specs():
                    if sp.agent == agent_name:
                        AGENT_TOKENS[sp.id] = token
                bot_id_env = config.get("discord_bot_id_env")
                if bot_id_env:
                    bot_id = os.environ.get(bot_id_env)
                    if bot_id:
                        DISCORD_ID_TO_AGENT[int(bot_id)] = agent_name


def load_permission_policy() -> tuple[list, list]:
    """Read permissions.allow / permissions.deny out of the shared claude
    settings file, for logging only — the CLI itself is what actually
    enforces the policy once --settings is on the spawn line. Missing file,
    missing "permissions" key, or a parse error all resolve to ([], []),
    the no-op default (#99: "An empty recall source must be a no-op" is
    #98's rule, but the same posture applies here — absence of policy is
    not an error)."""
    if not CLAUDE_SETTINGS_PATH.exists():
        return [], []
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"Could not parse {CLAUDE_SETTINGS_PATH} for permission policy: {e}")
        return [], []
    permissions = settings.get("permissions") or {}
    return permissions.get("allow") or [], permissions.get("deny") or []

# =============================================================================
# Session Management
# =============================================================================

async def get_or_create_session(agent: str) -> str:
    """Get existing session ID or create new one"""
    async with db.execute(
        "SELECT session_id FROM sessions WHERE agent = ?", (agent,)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            return row["session_id"]

    # Create new session
    session_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO sessions (agent, session_id) VALUES (?, ?)",
        (agent, session_id)
    )
    await db.commit()
    log.info(f"Created new session for {agent}: {session_id}")
    return session_id

async def session_started(agent: str) -> bool:
    """Whether the CLI has already begun this shard's current session id."""
    if db is None:
        return False
    async with db.execute(
        "SELECT started FROM sessions WHERE agent = ?", (agent,)
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and row["started"])


async def mark_session_started(agent: str):
    if db is None:
        return
    await db.execute("UPDATE sessions SET started = 1 WHERE agent = ? AND started = 0",
                     (agent,))
    await db.commit()


async def clear_session(agent: str):
    """Clear agent session and create new ID"""
    session_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO sessions (agent, session_id, input_tokens, compaction_count,
                              context_tokens, started)
        VALUES (?, ?, 0, 0, 0, 0)
        ON CONFLICT(agent) DO UPDATE SET
            session_id = ?,
            started = 0,
            input_tokens = 0,
            context_tokens = 0,
            compaction_count = 0,
            last_compacted = CURRENT_TIMESTAMP
        """,
        (agent, session_id, session_id)
    )
    await db.commit()
    agent_last_cost.pop(agent, None)
    log.info(f"Cleared session for {agent}, new ID: {session_id}")

async def update_session_tokens(agent: str, input_tokens: int):
    """Update session token count. `input_tokens` is the turn's uncached
    input only (the result event's field); it is NOT context size. See
    update_session_context."""
    await db.execute(
        "UPDATE sessions SET input_tokens = ? WHERE agent = ?",
        (input_tokens, agent)
    )
    await db.commit()

def usage_context_tokens(usage: Optional[Dict]) -> int:
    """Context size implied by one API call's usage block:
    input + cache_creation + cache_read, None as 0. Must be fed the LAST
    call of a turn, never the turn's summed usage."""
    if not usage:
        return 0
    return sum(usage.get(k) or 0 for k in (
        "input_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens"))

async def update_session_context(shard: str, tokens: int):
    """Record the session's context size (from the last main-thread API call).
    Keyed by shard id; a default shard has the agent's id."""
    await db.execute(
        "UPDATE sessions SET context_tokens = ?, "
        "context_updated_at = CURRENT_TIMESTAMP WHERE agent = ?",
        (tokens, shard)
    )
    await db.commit()

async def get_context_tokens() -> Dict[str, int]:
    """{shard: context_tokens} from the sessions table; 0 means unknown."""
    async with db.execute(
        "SELECT agent, context_tokens FROM sessions"
    ) as cursor:
        return {r["agent"]: r["context_tokens"] or 0 for r in await cursor.fetchall()}

async def session_cleared_at(shard: str):
    """sessions.last_compacted for the shard (naive UTC 'YYYY-MM-DD HH:MM:SS'),
    or None: a session created by get_or_create_session has never been cleared,
    so no handoff note can apply to it."""
    async with db.execute(
        "SELECT last_compacted FROM sessions WHERE agent = ?", (shard,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["last_compacted"] if row else None

# =============================================================================
# Agent Subprocess Management
# =============================================================================

def load_persona_files(agent: str) -> str:
    """Load and concatenate persona files for agent"""
    persona_dir = WORKSPACE_ROOT / "agents" / agent / "persona"
    if not persona_dir.exists():
        return ""

    persona_parts = []
    for file in sorted(persona_dir.glob("*.md")):
        with open(file) as f:
            content = f.read().strip()
            if content:
                persona_parts.append(content)

    return "\n\n".join(persona_parts)


def load_onboarding_prompt(agent: str) -> str:
    """Return the onboarding prompt iff persona is empty.

    Gated on persona content (not session-resume state) so wiping the DB
    doesn't retrigger onboarding once the user has given the agent its
    identity. Substitutes a small set of placeholders so the file can be
    shared across agent renames.
    """
    display_name = agent
    try:
        a = agent_registry.load_registry(WORKSPACE_ROOT).agent(agent)
        display_name = a.name or agent
        if a.role != "primary":
            return ""
    except Exception:
        pass  # registry unavailable: gate on persona content only

    persona_dir = WORKSPACE_ROOT / "agents" / agent / "persona"
    if persona_dir.exists() and any(
        f.read_text().strip() for f in persona_dir.glob("*.md") if f.is_file()
    ):
        return ""

    onboarding_path = WORKSPACE_ROOT / "agents" / agent / "onboarding.md"
    if not onboarding_path.exists():
        return ""

    # Paths need the id; prose gets the display name.
    text = onboarding_path.read_text().replace("agents/{{AGENT_NAME}}/", f"agents/{agent}/")
    substitutions = {
        "{{AGENT_NAME}}": display_name,
        "{{OWNER_NAME}}": os.environ.get("OWNER_NAME", "User"),
        "{{SYSTEM_NAME}}": os.environ.get("SYSTEM_NAME", "karakos"),
    }
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    return text.strip()


def load_memory_index(agent: str) -> str:
    """Load the routing-table-only memory index (MEMORY.md) if present."""
    memory_index = WORKSPACE_ROOT / "agents" / agent / "memory" / "MEMORY.md"
    if not memory_index.exists():
        return ""
    try:
        return memory_index.read_text().strip()
    except Exception as e:
        log.warning(f"Failed to read memory index for {agent}: {e}")
        return ""


STORED_FACTS_HEADER = "# Stored Facts (knowledge graph)"


def load_stored_facts(agent: str = "", limit: int = 50) -> str:
    """Top-N `fact` observations from the knowledge graph for the spawn prompt.

    Automated turns (heartbeats, pokes, scheduled jobs) skip the per-prompt
    recall hook, so durable facts also load once per spawn/resume. Read through
    lib/graph only; an uninitialised or unreadable graph yields "" (a fresh
    install spawns with no block). Distinct header: never confused with the
    hook's [ACTIVE RECALL] block.
    """
    try:
        from lib.graph.recall import top_facts
        from lib.graph.schema import GraphNotInitialised
        from lib.graph.store import open_graph
        store = open_graph(WORKSPACE_ROOT / "data", create=False)
        facts = top_facts(store, agent, limit)
    except GraphNotInitialised:
        return ""  # fresh install: no block, no log line
    except Exception as e:
        log.debug(f"No stored facts for {agent}: {e}")
        return ""
    lines = []
    for f in facts:
        text = " ".join(str(f["content"] or "").split())
        subject = f.get("subject")
        domain = f.get("domain")
        tag = f" [{domain}]" if domain and domain != "general" else ""
        lines.append(f"- **{subject}{tag}:** {text}" if subject else f"- {text}{tag}")
    if not lines:
        return ""
    return (STORED_FACTS_HEADER + "\n\nDurable knowledge recorded from previous interactions:\n\n"
            + "\n".join(lines))


async def start_agent_subprocess(shard: str):
    """Start the persistent Claude subprocess for a shard (an agent's default
    shard has the agent's id). Shards of one agent share persona and memory;
    each has its own session, prompt shard text and runtime state."""
    agent = STATE.agent_of(shard)
    config = STATE.cfg(shard)
    if not config:
        log.error(f"No config found for agent: {agent}")
        return
    label = label_of(shard)
    is_default_shard = shard == agent

    session_id = await get_or_create_session(shard)
    resume_session = await session_started(shard)
    # Core + agent section + shard text + house style, composed at spawn so a
    # fleet-wide rule is one edit. Never fails the spawn over a prompt file.
    system_prompt_text = prompt_compose.compose_system_prompt(
        WORKSPACE_ROOT, agent, None if is_default_shard else shard, config=config)
    prompt_compose.write_generated(WORKSPACE_ROOT, agent, system_prompt_text,
                                   None if is_default_shard else shard)

    # Load persona
    persona_content = load_persona_files(agent)

    # Load memory index (routing table if present)
    memory_index = load_memory_index(agent)
    if memory_index:
        persona_content = (
            memory_index + ("\n\n" + persona_content if persona_content else "")
        )

    # Load stored facts from persistent memory (learned facts layer)
    stored_facts = load_stored_facts(agent)
    if stored_facts:
        log.info(f"Injecting stored facts for {agent} into system prompt")
        persona_content = (
            stored_facts + ("\n\n" + persona_content if persona_content else "")
        )

    # First-boot gate: if no persona has been written yet, prepend the
    # onboarding prompt so the agent asks the user for guidance instead
    # of arriving fully-formed.
    # Only the agent's first shard: a second shard of an unonboarded agent must
    # not start a second onboarding.
    onboarding = (load_onboarding_prompt(agent)
                  if shards_lib.first_shard(effective_specs(), agent) == shard else "")
    if onboarding:
        log.info(f"Injecting onboarding prompt for {label} (persona is empty)")
        persona_content = onboarding + ("\n\n" + persona_content if persona_content else "")

    # Handoff note from the previous session (step 2.6). It rides in
    # --append-system-prompt, which a fresh session honours and a resumed one
    # ignores, so it is consumed only when the session was cleared after the
    # note was written; a reload simply leaves it on disk.
    note = sp_lib.consume_handoff(WORKSPACE_ROOT, shard,
                                  await session_cleared_at(shard), time.time())
    if note:
        log.info(f"Injecting handoff note for {label} ({len(note)} chars)")
        persona_content = (sp_lib.format_handoff_block(note)
                           + ("\n\n" + persona_content if persona_content else ""))

    # Build command
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--model", runtime_overrides.effective_model(WORKSPACE_ROOT, agent, config)[0],
        "--max-turns", str(config.get("max_turns", 200)),
        "--verbose",
        "--dangerously-skip-permissions",
        # An id the CLI has already begun is resumed; --session-id is only
        # valid for a brand-new one (the real CLI exits "already in use").
        "--resume" if resume_session else "--session-id", session_id,
        "--system-prompt", system_prompt_text,
    ]

    # Package-owned hook wiring (PreToolUse/PostToolUse/UserPromptSubmit/Stop/
    # SessionStart etc.) lives in this settings file rather than a `.claude/`
    # dir the installer would have to scaffold and the user could delete.
    # It also carries permissions.allow/permissions.deny (#99) — a reviewable,
    # version-controlled tool policy instead of ad hoc CLI flags. Note that
    # --dangerously-skip-permissions below does NOT override a deny rule: a
    # fully-denied tool is dropped from the session's tool list at spawn
    # (verified against the real CLI — see tests/test_permissions_policy.py),
    # and skip-permissions only removes the interactive-approval step for
    # tools that aren't denied. allow/deny apply to every agent that shares
    # this file; there is currently no per-agent settings file.
    if CLAUDE_SETTINGS_PATH.exists():
        cmd.extend(["--settings", str(CLAUDE_SETTINGS_PATH)])
        allow_list, deny_list = load_permission_policy()
        if allow_list or deny_list:
            log.info(f"{label} permission policy: allow={allow_list} deny={deny_list}")
    else:
        log.warning(f"No settings file at {CLAUDE_SETTINGS_PATH}; starting {label} with hooks unwired")

    if persona_content:
        cmd.extend(["--append-system-prompt", persona_content])

    # Effort (6.4): runtime override, else the registry key, else the CLI default.
    effort, _src = runtime_overrides.effective_effort(WORKSPACE_ROOT, agent, config)
    if effort and await asyncio.to_thread(runtime_overrides.cli_supports_effort):
        cmd.extend(["--effort", effort])

    # Steering (2.5): the CLI replays each user line it consumes, which is the
    # server's only signal of which steered lines the model has seen.
    if turn_loop.steering_on(STATE, shard):
        cmd.append("--replay-user-messages")
    STATE.pushback.pop(shard, None)
    STATE.replay_seen.pop(shard, None)

    # Add disallowed tools
    disallowed = config.get("disallowed_tools", [])
    for pattern in disallowed:
        cmd.extend(["--disallowedTools", pattern])

    # Add allowed tools if specified
    allowed = config.get("allowed_tools")
    if allowed:
        cmd.extend(["--allowedTools", ",".join(allowed)])

    # Per-agent environment (#99) — the registry's `env` dict is layered onto
    # an ALLOWLISTED slice of the server's environment (lib/spawn_env.py), not
    # the whole thing: Discord/API tokens stay out unless the agent's `env:`
    # names them (`${NAME}` pulls a value from the server env at spawn).
    # Per-agent entries win over the base; `extra` (identity) wins over both.
    env_overrides = config.get("env") or {}
    # KARAKOS_AGENT is identity: the MCP tool server runs as a child of this
    # subprocess and otherwise cannot say which agent is calling `ask_user`
    # (#101). WORKSPACE_ROOT and the agent-server address/token are for the
    # package's own hooks and MCP servers (tools-server/admin-server call back
    # into this server); they are not in the inert allowlist, so set them here.
    extra = {"KARAKOS_AGENT": agent, "KARAKOS_SHARD": shard, "WORKSPACE_ROOT": str(WORKSPACE_ROOT),
             "AGENT_SERVER_PORT": str(PORT)}
    if AGENT_SERVER_TOKEN:
        extra["AGENT_SERVER_TOKEN"] = AGENT_SERVER_TOKEN
    if "AGENT_SERVER_URL" in os.environ:
        extra["AGENT_SERVER_URL"] = os.environ["AGENT_SERVER_URL"]
    if spawn_env_lib.passthrough_requested(os.environ):
        spawn_env = {**os.environ, **spawn_env_lib.resolve_agent_env(env_overrides, os.environ, agent), **extra}
    else:
        spawn_env = spawn_env_lib.build_subprocess_env(os.environ, env_overrides, extra)
    redact.register_literals(spawn_env)     # secret values, masked wherever they appear
    if env_overrides:
        log.info(f"{label} env overrides: {sorted(env_overrides.keys())}")

    shard_note = "" if is_default_shard else f", shard={shard}"
    log.info(f"Starting {label} subprocess (model={config.get('model')}, "
             f"session={session_id[:8]}{shard_note})")

    # Cancel any stderr reader left over from a prior subprocess for this
    # agent before spawning a new one — otherwise it leaks on every respawn.
    stale_reader = stderr_reader_tasks.pop(shard, None)
    if stale_reader and not stale_reader.done():
        stale_reader.cancel()

    # Same for the respawn watcher (#90) — a watcher still awaiting the old
    # process would fire a spurious "exited unexpectedly" the moment that
    # process is reaped, describing a restart we are performing right here.
    # Except when the stale watcher IS this task: respawn_watcher calls us after
    # its own process died, and cancelling the current task would abort the
    # spawn at its first real await (found by the fake-claude harness; fakes
    # that never suspend in create_subprocess_exec hid it).
    stale_watcher = respawn_watcher_tasks.pop(shard, None)
    if (stale_watcher and not stale_watcher.done()
            and stale_watcher is not asyncio.current_task()):
        stale_watcher.cancel()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
        )
        agent_processes[shard] = proc
        agent_states[shard] = "IDLE"
        agent_sessions[shard] = session_id
        # A live process is by definition no longer deliberately dead. Clearing
        # here rather than in the kill paths keeps the flag correct for the one
        # kill that is *not* followed by a spawn — POST /kill, which is meant
        # to leave the agent down.
        deliberate_kills.discard(shard)

        # Start stderr reader, tracked so it can be cancelled on kill/respawn.
        stderr_reader_tasks[shard] = asyncio.create_task(stderr_reader(shard, proc))
        respawn_watcher_tasks[shard] = asyncio.create_task(respawn_watcher(shard, proc))

        log.info(f"{label} subprocess started (PID {proc.pid})")
    except Exception as e:
        log.error(f"Failed to start {label}: {e}")
        agent_states[shard] = "ERROR_RECOVERY"

async def stderr_reader(shard: str, proc: asyncio.subprocess.Process):
    """Read and log stderr from agent subprocess"""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode().strip()
            if msg:
                log.warning(f"{label_of(shard)} stderr: {msg}")
    except Exception as e:
        log.error(f"stderr reader error for {label_of(shard)}: {e}")

async def kill_agent_subprocess(shard: str):
    """Terminate agent subprocess"""
    # A killed caller cannot be waiting on a hive call (step 2.3).
    await hive_cancel_caller(shard)
    proc = agent_processes.get(shard)
    if not proc:
        return

    # Secondary guard (#90). The primary one is the watcher-cancel at the
    # bottom of this function, which wins every interleaving reachable in a
    # test: the watcher is suspended on proc.wait() and cancellation lands
    # before its scheduled wakeup runs. This flag covers the one window
    # cancellation cannot — a watcher that has already resumed and run past
    # its checks into start_agent_subprocess, where a cancel would abort a
    # spawn half-done rather than prevent it. Set before terminating, since a
    # flag set afterwards would lose that window by definition.
    deliberate_kills.add(shard)

    log.info(f"Killing {label_of(shard)} subprocess (PID {proc.pid})")
    # Snapshot the tree BEFORE signalling the root: once it dies its children
    # reparent to init and can no longer be found.
    # Only a real child process of ours: a test double's pid (FakeProcess(pid=1))
    # must never reach a real process-tree reap.
    tree = (procreap.snapshot_tree(proc.pid)
            if isinstance(proc, asyncio.subprocess.Process) else {})
    def _already_dead(exc: BaseException) -> bool:
        # The process exited between the lookup and the signal (or the
        # respawn watcher reaped it): nothing left to kill.
        return isinstance(exc, ProcessLookupError) or (
            isinstance(exc, OSError) and exc.errno == errno.ESRCH)

    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.warning(f"{label_of(shard)} didn't terminate, sending SIGKILL")
        try:
            proc.kill()
            await proc.wait()
        except OSError as e:
            if not _already_dead(e):
                raise
    except OSError as e:
        if not _already_dead(e):
            raise
        log.info(f"{label_of(shard)} subprocess already gone at kill time")

    agent_processes.pop(shard, None)
    # Lines the dead process never replayed go back to the queue (2.5).
    STATE.pushback.pop(shard, None)
    STATE.replay_seen.pop(shard, None)
    await turn_loop.release_pending(STATE, shard, "kill")

    # Whatever the tool calls left running (dev servers, nohup/setsid children).
    try:
        tree.pop(proc.pid, None)
        reaped = await asyncio.to_thread(procreap.reap, tree)
        n = len(reaped["termed"]) + len(reaped["killed"])
        if n:
            log.info(f"{label_of(shard)}: reaped {n} process(es) beyond the subprocess")
        if reaped["survived"]:
            log.warning(f"{label_of(shard)}: could not kill {reaped['survived']}")
    except Exception as e:
        log.warning(f"{label_of(shard)}: process reap failed: {type(e).__name__}: {e}")

    reader_task = stderr_reader_tasks.pop(shard, None)
    if reader_task and not reader_task.done():
        reader_task.cancel()

    watcher_task = respawn_watcher_tasks.pop(shard, None)
    if watcher_task and not watcher_task.done():
        watcher_task.cancel()

    log.info(f"{label_of(shard)} subprocess terminated")


async def notify_respawn(shard: str, reason: str, restarted: bool = True) -> None:
    """Post a one-line notice that the subprocess restarted, so a context
    reset is visible rather than reading as amnesia (#90).

    Goes to the last channel the agent spoke in. Never raises: a failed notice
    must not take down the respawn it is describing.

    `restarted=False` is the crashloop case — the agent is down and staying
    down, so the notice must not promise it is back.
    """
    channel_id = agent_last_channel.get(shard)
    if not channel_id or channel_id == "0":
        log.info(f"{label_of(shard)} respawn event ({reason}); no known channel to notify")
        return

    if restarted:
        notice = (
            f"🔄 {label_of(shard)} restarted — {reason}. Context was cleared; "
            f"recent conversation may need a recap."
        )
    else:
        notice = f"🛑 {label_of(shard)} is down — {reason}."
    try:
        # Deliberately not dead_letter=True. This is an incidental notice, not
        # a reply anyone is waiting on, and replaying it on a later boot would
        # announce a restart that had already been announced.
        await post_to_discord(shard, channel_id, notice)
    except Exception as e:
        log.warning(f"respawn notice for {label_of(shard)} failed: {e}")


async def respawn_watcher(shard: str, proc: asyncio.subprocess.Process):
    """Await this subprocess's exit and, if nobody asked for it, bring the
    agent back and say so (#90).

    Before this, a subprocess that died while idle was simply gone: nothing
    watched for it, so the agent stayed dead until the next message failed to
    send, and the reply after that had no memory of the conversation with no
    explanation offered. `stderr_reader` was the only task that observed the
    exit and it discarded the fact.

    The agent lock is held across the respawn so this cannot race a turn that
    is still unwinding — a mid-turn crash makes read_agent_response return on
    EOF, which releases the lock a moment later.
    """
    try:
        returncode = await proc.wait()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"respawn watcher for {label_of(shard)} failed to await exit: {e}")
        return

    if shutting_down or shard in deliberate_kills:
        return

    lock = agent_locks.get(shard)
    if lock is None:
        return

    async with lock:
        # Re-check under the lock. Both conditions can become true while we
        # waited for a turn to finish, and respawning after a deliberate kill
        # would resurrect an agent an operator just took down.
        if shutting_down or shard in deliberate_kills:
            return
        # Someone else already replaced this process; its lifecycle is theirs.
        if agent_processes.get(shard) is not proc:
            return

        # A call row the dead process was answering will never get a reply, and
        # a dead caller cannot be waiting on a call.
        await hive_cancel_caller(shard)
        await turn_loop.release_pending(STATE, shard, "exit")
        try:
            await msgqueue.write_commit(
                db,
                "UPDATE message_queue SET processed = ? WHERE agent = ? AND processed = ?"
                " AND call_id IS NOT NULL AND reply_to_agent IS NOT NULL",
                (STATUS_CRASHED, shard, STATUS_IN_PROGRESS))
        except Exception as e:
            log.warning(f"hive crash mark for {shard} failed: {e}")

        # Crashloop brake. A subprocess that dies immediately on spawn — bad
        # model name, missing MCP binary, unreadable settings file — would
        # otherwise respawn and announce itself forever, turning one broken
        # config into an unbounded stream of Discord messages. Recovery is
        # worth automating; an infinite loop is not.
        recent = respawn_history.setdefault(shard, [])
        now = time.monotonic()
        recent[:] = [t for t in recent if now - t < RESPAWN_WINDOW_SECONDS]
        recent.append(now)
        if len(recent) > RESPAWN_MAX_IN_WINDOW:
            log.error(
                f"{label_of(shard)} exited {len(recent)} times in {RESPAWN_WINDOW_SECONDS}s "
                f"(code {returncode}) — not respawning again"
            )
            await notify_respawn(
                shard,
                f"it crashed {len(recent)} times in under "
                f"{RESPAWN_WINDOW_SECONDS // 60} minutes and has been left down; "
                f"this needs a look at the server logs",
                restarted=False,
            )
            return

        log.warning(f"{label_of(shard)} subprocess exited unexpectedly (code {returncode}), respawning")
        await start_agent_subprocess(shard)
        if turn_loop.steering_on(STATE, shard):
            # Released steered rows are queued again: deliver them to the new process.
            turn_loop.spawn(STATE, turn_loop.drain_shard(STATE, shard))

    await notify_respawn(shard, f"the subprocess exited unexpectedly (code {returncode})")

async def restart_agent(shard: str):
    """Restart agent subprocess"""
    log.info(f"Restarting {label_of(shard)}")
    await kill_agent_subprocess(shard)
    await clear_session(shard)
    agent_last_cost.pop(shard, None)
    response_buffers[shard] = ""
    await start_agent_subprocess(shard)


async def reload_agent(shard: str):
    """Bounce the subprocess but keep the session — used to pick up new
    SYSTEM_PROMPT / persona / MCP config without dropping conversation
    context. The respawn calls --resume on the existing session_id.
    """
    log.info(f"Reloading {label_of(shard)} (preserving session)")
    await kill_agent_subprocess(shard)
    agent_last_cost.pop(shard, None)
    response_buffers[shard] = ""
    await start_agent_subprocess(shard)


async def interrupt_agent(shard: str) -> bool:
    """Stop an in-flight generation, keeping the session. Returns whether
    there was anything to stop.

    There is no "stop" message in Claude Code's stream-json protocol, so the
    only way to end a turn that is already running is to end the process
    carrying it. Killing it makes the `readline()` inside read_agent_response
    return EOF, which unwinds the turn, releases the agent's lock, and leaves
    the state IDLE — so the next message is picked up normally by the same
    path any message uses. The respawn resumes the same session_id, so the
    conversation survives.

    `interrupted_agents` marks the turn as abandoned: without it the partial
    text that had accumulated before the kill would be posted to Discord as
    if it were the answer, which is the opposite of what "interrupt" means.
    """
    if agent_states.get(shard) != "PROCESSING":
        return False

    log.info(f"Interrupting {label_of(shard)} (session preserved)")
    interrupted_agents.add(shard)
    await kill_agent_subprocess(shard)
    agent_last_cost.pop(shard, None)
    response_buffers[shard] = ""
    await start_agent_subprocess(shard)
    return True


async def flush_agent_queue(shard: str) -> int:
    """Drop every message still waiting for `agent`. Returns how many.

    In-progress messages are left alone: they are already inside the
    subprocess and deleting the row would only lose the record of them.
    """
    async with db.execute(
        "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
        (shard, STATUS_QUEUED),
    ) as cursor:
        row = await cursor.fetchone()
        pending = row["count"]

    if pending:
        await msgqueue.write_commit(
            db,
            "UPDATE message_queue SET processed = ? WHERE agent = ? AND processed = ?",
            (STATUS_SKIPPED, shard, STATUS_QUEUED),
        )

    log.info(f"Flushed {pending} queued message(s) for {label_of(shard)}")
    return pending

# =============================================================================
# Cost Tracking
# =============================================================================

async def post_cost_update(agent: str, metadata: Dict):
    """Post cost update to Discord and database"""
    session_total = metadata.get("total_cost_usd", 0.0)
    input_tokens = metadata.get("input_tokens", 0)
    output_tokens = metadata.get("output_tokens", 0)
    duration_ms = metadata.get("duration_ms", 0)
    # A conversation is one context window: the CLI's own session_id, which
    # only changes on a clear/respawn (see get_or_create_session /
    # clear_session). The result event carries it, but fall back to the
    # in-memory session map so a cost row is never left unattributed.
    session_id = metadata.get("session_id") or agent_sessions.get(agent)

    # Calculate delta
    last_cost = agent_last_cost.get(agent, 0.0)
    cost_delta = session_total - last_cost
    agent_last_cost[agent] = session_total

    # Store in database
    await msgqueue.write_commit(
        db,
        """
        INSERT INTO cost_events (agent, cost_delta, session_total, input_tokens, output_tokens, duration_ms, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (agent, cost_delta, session_total, input_tokens, output_tokens, duration_ms, session_id)
    )

    # Post to Discord cost channel (if configured)
    cost_channel_id = channels_config.get("channels", {}).get("cost", {}).get("id")
    if cost_channel_id and cost_delta > 0.001:
        duration_s = duration_ms / 1000.0
        message = f"`{agent}` +${cost_delta:.2f} (session: ${session_total:.2f}) • {input_tokens:,}in/{output_tokens:,}out • {duration_s:.1f}s"
        await post_to_discord(agent, cost_channel_id, message)

async def check_cost_limits(author_id: str) -> Dict[str, Any]:
    """Check if cost limits have been exceeded"""
    if author_id == OWNER_DISCORD_ID:
        return {"exceeded": False, "reason": "owner"}

    # Get daily cost
    async with db.execute(
        """
        SELECT SUM(cost_delta) as total
        FROM cost_events
        WHERE timestamp > datetime('now', '-1 day')
        """
    ) as cursor:
        row = await cursor.fetchone()
        daily_total = row["total"] or 0.0

    if daily_total >= COST_DAILY_LIMIT:
        return {"exceeded": True, "reason": "daily", "total": daily_total, "limit": COST_DAILY_LIMIT}

    # Get monthly cost
    async with db.execute(
        """
        SELECT SUM(cost_delta) as total
        FROM cost_events
        WHERE timestamp > datetime('now', '-30 days')
        """
    ) as cursor:
        row = await cursor.fetchone()
        monthly_total = row["total"] or 0.0

    if monthly_total >= COST_MONTHLY_LIMIT:
        return {"exceeded": True, "reason": "monthly", "total": monthly_total, "limit": COST_MONTHLY_LIMIT}

    return {"exceeded": False, "daily": daily_total, "monthly": monthly_total}

# =============================================================================
# Liveness beacons
# =============================================================================

# health-monitor.py reads staleness out of data/health/*.json, which catches a
# component that has stopped or crashed. It cannot see the failure that
# actually strands a user: a process that is alive and stuck. The claude
# subprocess is running, this server's event loop is fine, /health answers
# 200, the port is open — and the messages go nowhere.
#
# What distinguishes a wedge from an idle agent is not staleness alone. An
# idle agent writes nothing for hours and that is correct. A wedge is
# "claimed a turn, then went silent", so the beacon carries BOTH the state and
# the last activity time, and only the pair is diagnostic.
#
# The beacon is written here, by the loop that would go silent, and read by
# bin/wedge-check.py, which runs as a separate process on its own schedule. A
# check that runs inside the thing it is checking is not a check.

_last_beacon_write: Dict[str, float] = {}

# shard -> {phase, last_event_type, current_tool}: filled by the beacon hooks below.
beacon_extra: Dict[str, dict] = {}


def _beacon_diagnosis_fields(agent: str, state: str) -> dict:
    """The additive keys wedge-check/lib/stall.py read to say *why* a shard is
    silent. Every probe is guarded: a failure here must not cost the base beacon."""
    extra = beacon_extra.get(agent, {}) if state != "IDLE" else {}
    out = {
        "shard": agent,
        "proc_pid": None,
        "phase": extra.get("phase"),
        "last_event_type": extra.get("last_event_type"),
        "current_tool": extra.get("current_tool"),
        "held_until": None,
        "blocked_on": None,
        "ask_pending": state == ask_handler.AWAITING_USER_STATE,
        "paused": None,
    }
    for key, probe in (
        ("proc_pid", lambda: agent_processes[agent].pid if agent in agent_processes else None),
        ("held_until", lambda: agent_hold_notice_until.get(agent)),
        ("paused", lambda: _paused_entry(agent)),
        ("blocked_on", lambda: next(
            ({"call_id": cid, "callee": oc.callee}
             for cid, oc in STATE.hive.open.items() if oc.caller == agent), None)),
    ):
        try:
            out[key] = probe()
        except Exception:
            pass
    return out


def write_agent_beacon(agent: str, state: str, message_id: Optional[str] = None,
                       force: bool = False) -> None:
    """Record that this agent's processing loop is alive, and what it is doing.

    Best-effort and synchronous: it is a small write to a tmpfs-speed path,
    and it must never raise into the turn it is reporting on. A beacon that
    could crash a reply would be worse than no beacon.

    `force` bypasses the throttle for state transitions, which are the edges
    a watcher most needs to see promptly.
    """
    now = time.time()
    if not force and now - _last_beacon_write.get(agent, 0.0) < BEACON_MIN_INTERVAL_SEC:
        return
    _last_beacon_write[agent] = now

    try:
        AGENT_BEACON_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent": agent,
            "state": state,
            "last_activity": datetime.now().isoformat(),
            "message_id": message_id,
            "pid": os.getpid(),
        }
        payload.update(_beacon_diagnosis_fields(agent, state))
        path = AGENT_BEACON_DIR / f"{agent}.json"
        # Written via a temp file and renamed: the watcher is reading this
        # concurrently, and a partial write would read as corrupt — which the
        # watcher must not be able to mistake for a wedge.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except Exception as e:
        log.debug(f"Could not write liveness beacon for {agent}: {e}")


# =============================================================================
# Rate-limit headroom
# =============================================================================

# `cost_events` tracks dollars. Dollars are not what stops an agent
# mid-sentence — the rate limit is, and until now it was invisible until it
# fired, at which point the user saw a failed message with no explanation.
#
# The numbers come from the CLI itself. It emits `rate_limit_event` in the
# stream-json output — {status, resetsAt, rateLimitType, overageStatus,
# isUsingOverage} — so this is read in-band off a stream that is already open.
# Deliberately NOT a poller against the OAuth usage endpoint: that answers the
# same question on separate auth, on its own schedule, and is stale between
# polls, while this updates on every turn the limit changes.

# Nominal window lengths, so "resets at 03:15" can become "72% through the
# window". The CLI names the type; it does not give the length.
RATE_LIMIT_WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 86400,
}

# The CLI's own escalation. `allowed_warning` is it telling us headroom is
# running out; `rejected` is the limit already firing.
RATE_LIMIT_ALERT_STATUSES = frozenset({"allowed_warning", "rejected"})

# Fraction of the window elapsed at which we say so, once. The issue's
# acceptance test names 80%.
RATE_LIMIT_ALERT_FRACTION = 0.8


def rate_limit_window_progress(info, now=None):
    """Fraction (0.0–1.0) of the current rate-limit window that has elapsed.

    Returns None when it cannot be computed, which callers must render as
    "unknown" rather than as 0%. A missing `resetsAt`, an unrecognised
    `rateLimitType`, or a reset time already in the past all land here — and
    reporting any of them as "0% used" would be the same failure this issue
    is about, dressed as a number.
    """
    if not isinstance(info, dict):
        return None
    resets_at = info.get("resetsAt")
    window = RATE_LIMIT_WINDOW_SECONDS.get(info.get("rateLimitType"))
    if not isinstance(resets_at, (int, float)) or not window:
        return None

    now = time.time() if now is None else now
    remaining = resets_at - now
    if remaining <= 0:
        # The window is over; the next event will describe the new one.
        return None
    if remaining >= window:
        return 0.0
    return (window - remaining) / window


def format_usage_report(row, now=None):
    """Render a rate_limit_state row for a human. Never raises on a partial row."""
    if not row:
        return (
            "No rate-limit reading yet — the CLI reports headroom in-band, so "
            "this fills in the first time the agent takes a turn."
        )

    info = {
        "resetsAt": row["resets_at"],
        "rateLimitType": row["rate_limit_type"],
    }
    progress = rate_limit_window_progress(info, now=now)
    window_name = (row["rate_limit_type"] or "unknown").replace("_", "-")

    if progress is None:
        consumed = "window position unknown"
    else:
        consumed = f"{progress * 100:.0f}% through the {window_name} window"

    parts = [f"status `{row['status'] or 'unknown'}` — {consumed}"]

    if row["resets_at"]:
        now = time.time() if now is None else now
        remaining = int(row["resets_at"] - now)
        if remaining > 0:
            hours, minutes = divmod(remaining // 60, 60)
            parts.append(f"resets in {hours}h{minutes:02d}m")
        else:
            parts.append("window has reset")

    if row["is_using_overage"]:
        parts.append("currently on overage")
    elif row["overage_status"]:
        parts.append(f"overage {row['overage_status']}")

    return ", ".join(parts)


async def record_rate_limit_event(agent: str, info, now=None) -> None:
    """Persist the CLI's latest rate-limit reading and alert once per window.

    The alert is keyed on `resetsAt`, not on a boolean: a flag would fire once
    ever, and the limit is a recurring window. Keying on the reset timestamp
    means each new window can alert again, and the same window cannot.

    `resetsAt` is fixed for the life of a window, so the same value arrives on
    every event within it while the elapsed fraction climbs. That is why the
    column is stamped only when an alert is actually posted: stamping it on
    every write would mark a window as "already warned" during its quiet
    first hours and swallow the warning it was supposed to give at 80%.

    `now` is injectable so that progression through one window can be tested
    without waiting out the window.
    """
    if not isinstance(info, dict) or not info:
        return

    updates = rate_limits.parse_event(info, now=now)
    await rate_limits.upsert_windows(db, updates, now)
    usage_gate.update_breaker(STATE, await usage_gate.read_rate_rows(STATE))

    # `agent` is only wording below: the limit belongs to the account.
    for u in updates:
        progress = rate_limit_window_progress(
            {"resetsAt": u.resets_at, "rateLimitType": u.type}, now=now)
        should_alert = (
            u.status in RATE_LIMIT_ALERT_STATUSES
            or (progress is not None and progress >= RATE_LIMIT_ALERT_FRACTION)
        )
        if not should_alert:
            continue
        resets_at = int(u.resets_at) if u.resets_at is not None else None
        async with db.execute(
            "SELECT alerted_for_resets_at FROM rate_limit_state"
            " WHERE rate_limit_type = ?", (u.type,)
        ) as cursor:
            prior = await cursor.fetchone()
        if resets_at is not None and prior and prior["alerted_for_resets_at"] == resets_at:
            continue  # already said so for this window

        await msgqueue.write_commit(
            db,
            "UPDATE rate_limit_state SET alerted_for_resets_at = ?"
            " WHERE rate_limit_type = ?", (resets_at, u.type))

        consumed = ("in the warning band" if progress is None
                    else f"{progress * 100:.0f}% of the window's time elapsed")
        log.warning(f"{agent} rate-limit headroom low: type={u.type}, "
                    f"status={u.status}, {consumed}")

        channel_id = RATE_LIMIT_ALERT_CHANNEL_ID
        if channel_id and channel_id != "0":
            await post_to_discord(
                agent, channel_id,
                f"⚠️ rate-limit headroom low on the `{u.type}` window "
                f"(seen by `{agent}`) — status `{u.status}`, {consumed}."
            )


# =============================================================================
# Attachments
# =============================================================================

def _human_size(size) -> str:
    """Bytes as something an agent can reason about at a glance."""
    if not isinstance(size, (int, float)) or size < 0:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_attachments(raw) -> str:
    """Render a queued message's attachments as lines for the agent envelope.

    Returns "" when there are none, so callers can append unconditionally.

    Every attachment gets a line whether or not the relay managed to save it.
    The failure line is the point of the feature as much as the success line:
    before this, a message carrying a file reached the agent as bare text and
    the user got an answer that never acknowledged the file existed.
    """
    if not raw:
        return ""

    if isinstance(raw, str):
        try:
            attachments = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("Unparseable attachments column: %r", raw[:200])
            return ""
    else:
        attachments = raw

    if not isinstance(attachments, list) or not attachments:
        return ""

    lines = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        name = item.get("filename") or "unnamed"
        path = item.get("path")
        if path:
            descriptor = ", ".join(
                p for p in (item.get("content_type"), _human_size(item.get("size"))) if p
            )
            lines.append(f"  - {name} ({descriptor}) saved at: {path}")
        else:
            reason = item.get("skipped") or "not available"
            lines.append(f"  - {name} — NOT saved: {reason}")

    if not lines:
        return ""

    header = (
        f"  [{len(lines)} attachment(s) on this message. "
        "Open a saved one with the Read tool at the path given.]"
    )
    return "\n".join([header, *lines])


# =============================================================================
# Discord Integration
# =============================================================================

MAX_DISCORD_MSG_LEN = 2000

def split_discord_message(text: str, max_length: int = MAX_DISCORD_MSG_LEN) -> List[str]:
    """Split text into chunks Discord will accept (max 2000 chars each).

    Delegates to tengwar.split_for_discord, which cuts on line/space
    boundaries (hard cut when there are none), defers a whole fenced block to
    the next chunk when it fits, and closes/reopens ``` fences across every
    cut so no chunk carries a dangling fence. Discord rejects anything over
    2000 with a 400 and the message is lost, so the result is size-checked.
    """
    if not text or not text.strip():
        return []
    if len(text) <= max_length:
        return [text]

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
    return safe


# =============================================================================
# Outbox glue (step 6.1)
# =============================================================================
# One broken-store log per process: a reply is never lost to a broken store, it
# falls back to the direct path, but the operator hears about it once.
_outbox_conn = None
_outbox_wake: Optional[asyncio.Event] = None
_outbox_task: Optional[asyncio.Task] = None
_outbox_broken_logged = False
OUTBOX_CLOCK = time.time


def _outbox_now() -> float:
    return OUTBOX_CLOCK()


def _outbox_broken(what: str, exc: BaseException) -> None:
    global _outbox_broken_logged
    if not _outbox_broken_logged:
        _outbox_broken_logged = True
        log.error(f"Discord outbox unavailable ({what}: {type(exc).__name__}: {exc}); "
                  f"posting directly")


def _outbox_store(create: bool = True):
    """The open outbox connection, or None. `create=False` never creates the file."""
    global _outbox_conn
    if _outbox_conn is not None:
        return _outbox_conn
    if not create and not OUTBOX_PATH.is_file():
        return None
    try:
        _outbox_conn = outbox_lib.open_store(OUTBOX_PATH)
    except Exception as e:
        _outbox_broken("open", e)
        return None
    return _outbox_conn


def _outbox_notify() -> None:
    if _outbox_wake is not None:
        _outbox_wake.set()


def dead_letter_count() -> int:
    """Rows in the outbox that gave up (`dead`). Kept under its old name for /health."""
    conn = _outbox_store(create=False)
    if conn is None:
        return 0
    try:
        return outbox_lib.stats(conn, _outbox_now())["dead"]
    except Exception as e:
        _outbox_broken("stats", e)
        return 0


def split_discord_message_visible(text: str) -> List[str]:
    """Chunks of `text` that have something visible in them."""
    return [c for c in split_discord_message(text) if post_guard.has_visible(c)]


async def _post_direct(agent: str, token: str, channel_id: str, content: str,
                       reply_to: Optional[str] = None, flags: int = 0) -> Optional[str]:
    """Direct, in-turn delivery: retried POST_MAX_ATTEMPTS times per chunk.

    Used for incidental posts (tool lines, notices) and as the fallback when
    the outbox store is unusable. `content` is already rendered; blank chunks
    are dropped here, so this path cannot post an empty message.
    Returns the last message id, or None if nothing was posted or any chunk failed.
    """
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json"
    }
    chunks = [c for c in split_discord_message(content) if post_guard.has_visible(c)]
    if not chunks:
        log.info(f"skip post (empty) agent={agent} channel={channel_id}")
        return None
    last_msg_id = None
    failed = 0

    for idx, chunk in enumerate(chunks):
        payload = {"content": chunk}
        if flags:
            payload["flags"] = flags
        # Only reply-reference the first chunk
        if reply_to and last_msg_id is None:
            payload["message_reference"] = {"message_id": reply_to}

        posted = False
        attempt = 0
        while attempt < POST_MAX_ATTEMPTS and not posted:
            attempt += 1
            try:
                async with http_session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        last_msg_id = data.get("id")
                        posted = True
                    elif resp.status == 429:
                        retry_after = (await resp.json()).get("retry_after", 1)
                        log.warning(
                            f"Rate limited posting to {channel_id}, retry after {retry_after}s "
                            f"(attempt {attempt}/{POST_MAX_ATTEMPTS})"
                        )
                        await asyncio.sleep(retry_after)
                    elif 400 <= resp.status < 500:
                        # Permission revoked, token rejected, channel gone, bad
                        # body. None of these clear by trying again.
                        log.error(
                            f"Discord API error {resp.status} on chunk "
                            f"{idx + 1}/{len(chunks)} ({len(chunk)} chars); "
                            f"not retryable"
                        )
                        break
                    else:
                        log.error(
                            f"Discord API error {resp.status} on chunk "
                            f"{idx + 1}/{len(chunks)} ({len(chunk)} chars) "
                            f"(attempt {attempt}/{POST_MAX_ATTEMPTS}): "
                            f"{await resp.text()}"
                        )
                        if attempt < POST_MAX_ATTEMPTS:
                            await asyncio.sleep(POST_RETRY_BASE_SEC * attempt)
            except Exception as e:
                log.error(
                    f"Error posting chunk {idx + 1}/{len(chunks)} to Discord "
                    f"(attempt {attempt}/{POST_MAX_ATTEMPTS}): {e}"
                )
                if attempt < POST_MAX_ATTEMPTS:
                    await asyncio.sleep(POST_RETRY_BASE_SEC * attempt)

        if not posted:
            failed += 1

    # A chunk that never landed is a piece of the reply the user will never
    # see. Returning the id of a sibling chunk reports the whole message as
    # delivered and the loss goes unnoticed.
    if failed:
        log.error(
            f"post_to_discord: {failed} of {len(chunks)} chunk(s) failed for "
            f"{agent} in {channel_id}; message is incomplete"
        )
        return None
    return last_msg_id


def _discord_error_detail(status: Optional[int], body: str) -> str:
    """`HTTP <status>` plus Discord's own error message/code, never the request content."""
    out = f"HTTP {status}"
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            if j.get("code") is not None:
                out += f" code={j.get('code')}"
            if isinstance(j.get("message"), str):
                out += f" {j['message'][:100]}"
    except Exception:
        pass
    return out


MAX_OUTBOUND_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
MAX_OUTBOUND_ATTACHMENTS = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "10"))
# Agents may attach only files they put in this folder, never the rest of data/
# (which holds the message store and other state).
OUTGOING_ATTACHMENT_DIR = WORKSPACE_ROOT / "data" / "outgoing"


def _attachment_form(payload: Dict[str, Any], paths: List[str]) -> "aiohttp.FormData":
    """Multipart body for a message with files: `payload_json` plus `files[n]`.

    Files are read when the request is built, so a retry re-reads them; a path
    that has gone missing raises FileNotFoundError (the sender treats that as
    permanent).
    """
    form = aiohttp.FormData()
    form.add_field("payload_json", json.dumps(payload), content_type="application/json")
    total = 0
    for n, path in enumerate(paths):
        data = Path(path).read_bytes()
        total += len(data)
        if total > MAX_OUTBOUND_ATTACHMENT_BYTES:   # grew since it was checked
            raise OSError("attachments grew past the size limit")
        form.add_field(f"files[{n}]", data,
                       filename=Path(path).name, content_type="application/octet-stream")
    return form


async def outbox_send_row(row: Dict[str, Any]) -> Optional[str]:
    """Send one claimed (`sending`) outbox row, from `chunks_done` onward.

    One POST per chunk. Every payload carries a per-chunk nonce with
    enforce_nonce, so a POST that succeeded just before a crash is deduplicated
    by Discord on the retry (delivery is at-least-once, narrowed by the nonce).
    Records each delivered chunk, then marks the row delivered and returns the
    last message id; a failure records its kind and returns None (the row goes
    back to pending with backoff, or dead). Tokens come from AGENT_TOKENS and
    never touch the database.
    """
    conn = _outbox_store()
    if conn is None:
        return None
    rid, agent, channel_id = row["id"], row["agent"], row["channel_id"]
    token = AGENT_TOKENS.get(agent) or (next(iter(AGENT_TOKENS.values()), None))
    if not token:
        outbox_lib.record_failure(conn, rid, "retry", None, "no Discord token configured",
                                  None, _outbox_now())
        return None
    chunks = [c for c in split_discord_message(row["content"]) if post_guard.has_visible(c)]
    if not chunks:
        log.info(f"skip post (empty) agent={agent} channel={channel_id}")
        outbox_lib.record_failure(conn, rid, "permanent", None, "empty after chunking", None,
                                  _outbox_now())
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    ids = json.loads(row.get("message_ids") or "[]")
    for idx in range(row.get("chunks_done") or 0, len(chunks)):
        payload: Dict[str, Any] = {
            "content": chunks[idx],
            "nonce": f"{rid[3:15]}-{idx}",
            "enforce_nonce": True,
        }
        if idx == 0 and row.get("reply_to"):
            payload["message_reference"] = {"message_id": row["reply_to"]}
        if row.get("flags"):
            payload["flags"] = row["flags"]
        status: Optional[int] = None
        retry_after = None
        detail = ""
        files_too_big = False
        try:
            request_kwargs: Dict[str, Any] = {"json": payload}
            files = json.loads(row.get("attachments") or "[]") if idx == 0 else []
            if files:
                payload["attachments"] = [{"id": n, "filename": Path(f).name}
                                          for n, f in enumerate(files)]
                try:
                    # checked again now: the file may have been swapped or grown
                    # (or a symlink planted) since the reply was queued
                    files = outbox_lib.resolve_attachments(
                        files, OUTGOING_ATTACHMENT_DIR, MAX_OUTBOUND_ATTACHMENT_BYTES,
                        MAX_OUTBOUND_ATTACHMENTS)
                    form = _attachment_form(payload, files)
                except (OSError, outbox_lib.AttachmentError) as e:
                    log.error(f"Discord outbox: attachment unreadable for {rid}: {e}")
                    outbox_lib.record_failure(conn, rid, "permanent", None,
                                              f"attachment unreadable: {type(e).__name__}",
                                              None, _outbox_now())
                    return None
                # multipart sets its own Content-Type (with the boundary)
                headers = {"Authorization": headers["Authorization"]}
                request_kwargs = {"data": form}
            async with http_session.post(url, headers=headers, **request_kwargs) as resp:
                status = resp.status
                if status in (200, 201):
                    data = await resp.json()
                    msg_id = str(data.get("id"))
                    outbox_lib.record_chunk(conn, rid, msg_id, _outbox_now())
                    ids.append(msg_id)
                    continue
                if status == 429:
                    try:
                        retry_after = (await resp.json()).get("retry_after")
                    except Exception:
                        retry_after = None
                    if retry_after is None:
                        retry_after = getattr(resp, "headers", {}).get("Retry-After")
                    try:
                        retry_after = float(retry_after) if retry_after is not None else None
                    except (TypeError, ValueError):
                        retry_after = None
                    detail = "HTTP 429 rate limited"
                else:
                    try:
                        body = await resp.text()
                    except Exception:
                        body = ""
                    detail = _discord_error_detail(status, body)
                    files_too_big = status == 413 and bool(files)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status, detail = None, f"{type(e).__name__}: {str(e)[:100]}"
        if files_too_big:
            # Discord's bot upload limit is not fixed; do not lose the reply text with
            # the files. Send this chunk once more without them, saying why.
            log.warning(f"Discord outbox: files refused as too large for {rid}; sending text only")
            note = "\n\n(files not sent: too large for Discord)"
            text = chunks[idx] + note if len(chunks[idx]) + len(note) <= MAX_DISCORD_MSG_LEN \
                else chunks[idx]
            plain = {k: v for k, v in payload.items() if k != "attachments"}
            plain["content"] = text
            try:
                async with http_session.post(url, headers=headers, json=plain) as resp2:
                    if resp2.status in (200, 201):
                        msg_id = str((await resp2.json()).get("id"))
                        outbox_lib.record_chunk(conn, rid, msg_id, _outbox_now())
                        ids.append(msg_id)
                        continue
                    status, detail = resp2.status, _discord_error_detail(
                        resp2.status, await resp2.text())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status, detail = None, f"{type(e).__name__}: {str(e)[:100]}"
        kind = outbox_lib.classify(status)
        log.error(f"Discord outbox: chunk {idx + 1}/{len(chunks)} for {rid} failed ({detail})")
        outbox_lib.record_failure(conn, rid, kind, status, detail, retry_after, _outbox_now())
        _outbox_notify()
        return None
    outbox_lib.mark_delivered(conn, rid, _outbox_now())
    last = ids[-1] if ids else None
    if last and row.get("queue_message_id"):
        await _writeback_discord_id(row["queue_message_id"], last)
    return last


async def _writeback_discord_id(queue_message_id: str, discord_id: str) -> None:
    """Record a delivered reply's Discord id on its message_queue row(s), so the
    crash sweep never sees it as unposted. Best effort: never blocks delivery.
    Rows of the same turn (same agent, channel, response, processed_at) share
    the reply, so they are marked together."""
    try:
        await msgqueue.write_commit(
            db,
            "UPDATE message_queue SET discord_response_id = ? WHERE discord_response_id IS NULL "
            "AND (message_id = ? OR (processed_at IS NOT NULL AND (agent, channel_id, response, processed_at) = "
            "(SELECT agent, channel_id, response, processed_at FROM message_queue "
            "WHERE message_id = ? AND processed_at IS NOT NULL)))",
            (discord_id, queue_message_id, queue_message_id))
    except Exception as e:
        log.warning(f"Discord outbox: could not write id back for {queue_message_id} "
                    f"({type(e).__name__}: {e})")


async def outbox_pass(now: Optional[float] = None) -> int:
    """One loop pass: claim every due row and send it. Returns rows attempted."""
    conn = _outbox_store(create=False)
    if conn is None:
        return 0
    try:
        rows = outbox_lib.claim_due(conn, _outbox_now() if now is None else now)
    except Exception as e:
        _outbox_broken("claim", e)
        return 0
    for row in rows:
        try:
            await outbox_send_row(row)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Discord outbox: sending {row['id']} raised {type(e).__name__}: {e}")
            try:
                outbox_lib.record_failure(conn, row["id"], "retry", None, type(e).__name__,
                                          None, _outbox_now())
            except Exception:
                pass
    return len(rows)


async def outbox_loop() -> None:
    """Background delivery: wait for an enqueue or the next retry time, send due rows."""
    global _outbox_wake
    _outbox_wake = asyncio.Event()
    conn = _outbox_store(create=False)
    if conn is not None:
        try:
            n = outbox_lib.recover_sending(conn, _outbox_now())
            log.debug(f"outbox: store opened, {n} row(s) recovered")
        except Exception as e:
            _outbox_broken("recover", e)
    while True:
        _outbox_wake.clear()
        try:
            if await outbox_pass():
                continue
            timeout = 30.0
            conn = _outbox_store(create=False)
            if conn is not None:
                nxt = outbox_lib.next_due(conn)
                if nxt is not None:
                    timeout = max(0.05, min(nxt - _outbox_now(), 30.0))
            try:
                await asyncio.wait_for(_outbox_wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Discord outbox loop error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


# -- Optional Discord behaviours (6.2) ---------------------------------------

def ux_config() -> "discord_ux.UxConfig":
    """The parsed "ux" blocks of channels_config, re-parsed whenever the config
    object is replaced (load_config, the reload route). Warnings log once."""
    if _ux_parsed["src"] is not channels_config or _ux_parsed["cfg"] is None:
        cfg, warnings = discord_ux.parse_ux(channels_config)
        for w in warnings:
            log.warning(f"channels.json ux: {w}")
        _ux_parsed["src"], _ux_parsed["cfg"] = channels_config, cfg
    return _ux_parsed["cfg"]


def _listed_channel_name(channel_id) -> Optional[str]:
    cid = str(channel_id)
    for name, cfg in (channels_config.get("channels") or {}).items():
        if isinstance(cfg, dict) and str(cfg.get("id")) == cid:
            return name
    return None


def ux_for_channel_id(channel_id) -> "discord_ux.ChannelUx":
    """Switches for a channel id; a registered bot thread inherits its parent's."""
    name = _listed_channel_name(channel_id)
    if name is None:
        parent = ux_threads.get(channel_id)
        if parent is not None:
            name = _listed_channel_name(parent)
    return ux_config().for_channel(name)


def ux_thread_cfg(channel_id) -> Optional["discord_ux.ThreadCfg"]:
    """Thread settings for a turn in `channel_id`, or None. A thread cannot be
    created inside a thread or a DM, so only a channel listed in channels.json
    under this very id qualifies."""
    name = _listed_channel_name(channel_id)
    if name is None:
        return None
    return ux_config().for_channel(name).threads


def ux_tool_streaming(channel_id) -> Optional[bool]:
    """A channel's `ux.tool_streaming` override, or None to inherit the agent's
    setting. Never raises: a UX lookup must not cost the turn."""
    try:
        return ux_for_channel_id(channel_id).tool_streaming
    except Exception:
        return None


def _ux_now() -> float:
    return time.time()


async def ux_create_thread(agent: str, channel_id: str, message_id: Optional[str],
                           name: str) -> Optional[str]:
    """Create a public thread on a message. Returns its id, or None (never
    raises): the caller then keeps posting in the channel."""
    if not message_id:
        return None
    token = AGENT_TOKENS.get(agent) or next(iter(AGENT_TOKENS.values()), None)
    if not token:
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/threads"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with http_session.post(url, headers=headers, json={
                "name": name, "auto_archive_duration": 60}) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                thread_id = str(data.get("id") or "")
                if thread_id:
                    ux_threads[thread_id] = str(channel_id)
                    return thread_id
                return None
            if resp.status in (400, 403):
                now = _ux_now()
                last = _ux_warned.get(str(channel_id))
                if last is None or now - last >= UX_WARN_EVERY_S:
                    _ux_warned[str(channel_id)] = now
                    log.warning(
                        f"cannot create thread in channel {channel_id} (HTTP {resp.status}); "
                        f"needs Create Public Threads and Send Messages in Threads. "
                        f"Tool lines stay in the channel")
            else:
                log.info(f"thread creation in {channel_id} failed: HTTP {resp.status}")
    except Exception as e:
        log.info(f"thread creation in {channel_id} failed: {type(e).__name__}: {e}")
    return None


async def add_reaction(agent: str, channel_id: str, message_id: str, emoji_path: str) -> bool:
    """Add `emoji_path` (already percent-encoded, see discord_ux.emoji_path) as
    the agent's own reaction. Single attempt, one retry after a 429. Never
    raises: a reaction is an acknowledgement, and losing one costs nothing."""
    token = AGENT_TOKENS.get(agent) or (list(AGENT_TOKENS.values())[0] if AGENT_TOKENS else None)
    if not token or not channel_id or channel_id == "0":
        log.info(f"skip reaction (no token or channel) agent={agent}")
        return False
    url = (f"https://discord.com/api/v10/channels/{channel_id}/messages/"
           f"{message_id}/reactions/{emoji_path}/@me")
    try:
        for attempt in (1, 2):
            async with http_session.put(url, headers={"Authorization": f"Bot {token}"}) as resp:
                if resp.status in (200, 204):
                    return True
                if resp.status == 429 and attempt == 1:
                    wait = min(float((await resp.json()).get("retry_after", 1)), 5.0)
                    await asyncio.sleep(wait)
                    continue
                log.warning(f"reaction by {agent} in {channel_id} failed: "
                            f"{_discord_error_detail(resp.status, await resp.text())}")
                return False
    except Exception as e:
        log.warning(f"reaction by {agent} in {channel_id} failed: {type(e).__name__}: {e}")
    return False


async def post_to_discord(agent: str, channel_id: str, content: str,
                          reply_to: Optional[str] = None,
                          dead_letter: bool = False,
                          queue_message_id: Optional[str] = None) -> Optional[str]:
    """Post message to Discord as agent, splitting if over 2000 chars.

    Empty/whitespace text and a reply that is exactly PASS are never posted
    (post_guard): returns None, no HTTP request, no outbox row, one INFO line.

    `dead_letter=True` means *durable*: the content is a reply someone is
    waiting on, so it goes into the outbox (data/outbox/outbox.db) and survives
    outages and restarts. The inline caller owns the row and runs one delivery
    pass, returning the last message id if every chunk landed, else None (the
    row is back in the outbox with backoff, not lost). If an older row for the
    same (agent, channel) is waiting, the new one is queued behind it and None
    is returned at once, so a fresh reply never overtakes one in backoff. If
    the store is unusable the call falls back to the direct path.
    Delivery is at-least-once, narrowed by a per-chunk nonce.
    `queue_message_id` is the originating message_queue row; the outbox stores
    it so the crash sweep finds the reply by key, and delivery writes the
    Discord id back to that row.

    With `dead_letter=False` (tool lines, cost updates, notices) the direct
    path runs: DISCORD_POST_MAX_ATTEMPTS tries per chunk, nothing stored.
    """
    global http_session

    # Skip posting if channel_id is "0" (silent mode)
    if channel_id == "0":
        return None

    # `[[attach: path]]` lines become file attachments on durable replies only.
    attachments: List[str] = []
    if dead_letter and "[[attach:" in (content or ""):
        content, attachments, notes = outbox_lib.extract_attachments(
            content, OUTGOING_ATTACHMENT_DIR, MAX_OUTBOUND_ATTACHMENT_BYTES,
            MAX_OUTBOUND_ATTACHMENTS)
        for note in notes:
            log.warning(f"outbound attachment refused agent={agent}: {note}")
        if notes:
            content = (content + "\n\n" if content else "") + "\n".join(
                f"(attachment not sent: {n})" for n in notes)
        if attachments and not content.strip():
            content = "Attached: " + ", ".join(Path(a).name for a in attachments)

    # Get agent's Discord token, fallback to primary agent
    token = AGENT_TOKENS.get(agent)
    prefix = ""
    if not token:
        # Use first available token as fallback
        if AGENT_TOKENS:
            token = list(AGENT_TOKENS.values())[0]
            prefix = f"[{agent}] "
        else:
            log.warning(f"No Discord tokens configured, cannot post for {agent}")
            return None

    # Discord does not render Markdown tables; turn them into a monospace
    # block first (no PNG: this path only sends JSON). Rendering can empty a
    # message, so the guard runs on the rendered text.
    rendered, _ = tengwar.render_for_discord(content)
    ok, reason = post_guard.post_decision(rendered)
    if not ok:
        log.info(f"skip post ({reason}) agent={agent} channel={channel_id}")
        return None
    rendered = prefix + rendered
    chunks = split_discord_message_visible(rendered)
    if not chunks:
        log.info(f"skip post (empty) agent={agent} channel={channel_id}")
        return None

    # Suppress-embeds (6.2): text posts only; post_discord_payload never sets it.
    flags = discord_ux.SUPPRESS_EMBEDS if ux_for_channel_id(channel_id).suppress_embeds else 0

    if dead_letter:
        conn = _outbox_store()
        if conn is not None:
            try:
                rid, claimed = outbox_lib.enqueue(
                    conn, agent, channel_id, rendered, reply_to=reply_to, flags=flags,
                    claimed=True,
                    now=_outbox_now(), content_sha=outbox_lib.content_sha(content),
                    chunks_total=len(chunks),
                    queue_message_id=queue_message_id,
                    attachments=attachments)
            except Exception as e:
                _outbox_broken("enqueue", e)
            else:
                _outbox_notify()
                if not claimed:
                    return None
                # No direct fallback from here: some chunks may already be out.
                # A store error leaves the row `sending`; recover_sending
                # re-sends it at boot with the same nonces.
                try:
                    row = outbox_lib.get(conn, rid)
                    return await outbox_send_row(row) if row else None
                except Exception as e:
                    _outbox_broken("inline send", e)
                    return None

    return await _post_direct(agent, token, channel_id, rendered, reply_to, flags)


def gateway_agent() -> Optional[str]:
    """The agent whose bot token bin/relay.py logs in with.

    This matters for #101 and only for #101. A button click is delivered over
    the gateway to the application that posted the message, and the relay
    holds exactly one gateway connection — opened with the first agent in
    agents.yaml that has a token (bin/relay.py::main). A question posted
    under any other agent's token renders fine and is then simply
    unclickable: Discord has nowhere to deliver the interaction. So the ask
    embed goes out under this token regardless of which agent asked, and the
    embed footer carries the real asker's name.

    The selection rule is duplicated rather than shared because the two
    processes do not import each other; both walk agent_config in file order
    and take the first entry with a configured token.
    """
    for name in agent_config:
        if name in AGENT_TOKENS:
            return name
    return None


async def post_discord_payload(agent: str, channel_id: str,
                               payload: Dict[str, Any]) -> Optional[str]:
    """POST a raw Discord message body (embeds, components) to a channel.

    post_to_discord() only knows how to send text and would drop the
    components, which are the entire point of an ask. Returns the message id
    or None; deliberately single-attempt, because the caller is a person
    waiting on a question and a slow retry loop is worse than a fast failure
    it can report.
    """
    if not channel_id or channel_id == "0":
        return None
    # Same rule as post_to_discord: a body with no visible text, no embeds and
    # no components is an empty post, which Discord answers with a 400.
    if not (post_guard.has_visible(payload.get("content")) or payload.get("embeds")
            or payload.get("components")):
        log.info(f"skip post (empty) agent={agent} channel={channel_id}")
        return None
    token = AGENT_TOKENS.get(agent)
    if not token:
        log.warning(f"No Discord token for {agent}; cannot post interactive message")
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with http_session.post(url, headers=headers, json=payload) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return data.get("id")
            body = (await resp.text())[:200]
            log.error(f"Discord API error {resp.status} posting interactive message: {body}")
            return None
    except Exception as e:
        log.error(f"Error posting interactive message to {channel_id}: {e}")
        return None


def should_post_tool_line(lines_posted: int, last_at: Optional[float],
                          now: float) -> bool:
    """Whether this turn may post another tool activity line right now (#91).

    `last_at is None` means "nothing posted yet this turn", and that case is
    never delayed: a turn that says nothing for the first interval is exactly
    the silence the issue is about.

    It is an explicit None and not a 0.0 sentinel, which is a distinction
    with a real failure behind it. `now - 0.0 >= interval` is true only
    because time.monotonic() is boot-relative and therefore large on a
    long-running host — 26694.2 on the box this was written on, against a 5
    second interval. In a container in its first minutes, the value the
    package actually ships into, it is small, and the sentinel form
    swallows the first line of the first turn. A mutation removing the
    first-call exemption survived the test suite for precisely this reason
    before the check was pulled out here where its inputs can be named.
    """
    if lines_posted >= TOOL_EVENT_MAX_PER_TURN:
        return False
    if last_at is None:
        return True
    return now - last_at >= TOOL_EVENT_MIN_INTERVAL


def describe_tool_call(tool_name: str, tool_input: Optional[Dict]) -> str:
    """One-line "⚙ Bash — npm test" summary of a stream-json tool_use block.

    The tool name alone does not answer the question these lines exist to
    answer. "⚙ Bash" nine times is barely more informative than silence;
    "⚙ Bash — npm test" tells the watcher the turn is moving and roughly
    where it is (#91).

    The argument picked per tool is the one a human would read first. An
    unknown tool degrades to the bare name rather than dumping its input —
    tool inputs carry file contents, patch bodies and credentials, and this
    reaches both a Discord channel (#91) and the dashboard chat page. Both
    surfaces go through here so neither can be redacted less than the other:
    the chosen detail is masked (lib/redact.py) before it is truncated, so a
    token is not cut in half and left unmatched.
    """
    name = str(tool_name or "unknown")
    detail = ""

    if isinstance(tool_input, dict):
        # Ordered: first key present wins, so Edit reports its path rather
        # than its patch body.
        for key in ("command", "file_path", "path", "pattern", "url",
                    "query", "description", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break

    if detail:
        detail = redact.redact_text(" ".join(detail.split()))
        if len(detail) > TOOL_EVENT_DETAIL_CHARS:
            detail = detail[:TOOL_EVENT_DETAIL_CHARS - 1].rstrip() + "…"
        # Backticks and newlines would break out of the subtext line.
        detail = detail.replace("`", "'")
        return f"⚙ {name} — {detail}"

    return f"⚙ {name}"


def summarize_tool_call(tool_name: str, tool_input: Optional[Dict]) -> str:
    """describe_tool_call() as a Discord subtext line (#91).

    The `-# ` prefix is Discord markup and belongs to that surface only —
    the dashboard's turn-events pill renders describe_tool_call() directly
    and would otherwise show a literal "-#".
    """
    return f"-# {describe_tool_call(tool_name, tool_input)}"


async def write_turn_event(message_ids: List[str], seq: int, kind: str, content: str) -> None:
    """Insert one turn_events row per message_id in this turn's batch.

    A "turn" can cover several queued message_ids at once (#121); writing
    the row once per id means whichever message_id the dashboard is polling
    with (see /api/chat/stream) sees it, the same broadcast pattern
    write_streaming_response uses for the response text itself. Best-effort:
    a bookkeeping failure must never cost the agent's actual reply, which is
    still arriving on this same loop.
    """
    if not message_ids or db is None:
        return
    content = redact.redact_text(content)
    try:
        def job(conn):
            try:
                conn.executemany(
                    "INSERT INTO turn_events (message_id, seq, kind, content) VALUES (?, ?, ?, ?)",
                    [(mid, seq, kind, content) for mid in message_ids])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        await db._execute(job, db._conn)  # one hop: no write lock held across an await
    except Exception as e:
        log.warning(f"turn_events insert failed: {e}")


async def start_typing(agent: str, channel_id: str):
    """Start typing indicator in Discord channel"""
    if channel_id == "0" or channel_id in typing_tasks:
        return

    async def typing_loop():
        token = AGENT_TOKENS.get(agent)
        if not token and AGENT_TOKENS:
            token = list(AGENT_TOKENS.values())[0]
        if not token:
            return

        url = f"https://discord.com/api/v10/channels/{channel_id}/typing"
        headers = {"Authorization": f"Bot {token}"}

        while True:
            try:
                async with http_session.post(url, headers=headers) as resp:
                    if resp.status != 204:
                        break
                await asyncio.sleep(TYPING_INTERVAL)
            except Exception:
                break

    task = asyncio.create_task(typing_loop())
    typing_tasks[channel_id] = task

async def stop_typing(channel_id: str):
    """Stop typing indicator"""
    task = typing_tasks.pop(channel_id, None)
    if task:
        task.cancel()

# =============================================================================
# Message Processing
# =============================================================================

async def send_to_agent(agent: str, content: str, message_ids: List[str]):
    """Send message to agent subprocess"""
    return await turn_loop.write_user_line(STATE, agent, content, message_ids)

async def write_streaming_response(message_ids: List[str], text: str) -> None:
    """Push partial response text into message_queue so SSE polling sees it.

    The /api/chat/stream SSE route reads message_queue.response and forwards
    deltas to the dashboard. Without these incremental writes, the dashboard
    only sees text on the post-loop UPDATE — i.e., never until the turn ends.
    """
    if not message_ids or db is None:
        return
    placeholders = ",".join("?" * len(message_ids))
    try:
        await msgqueue.write_commit(
            db,
            f"UPDATE message_queue SET response = ? WHERE message_id IN ({placeholders})",
            (text, *message_ids),
        )
    except Exception as e:
        log.warning(f"streaming response write failed: {e}")


async def write_partial_response(message_ids: List[str], text: str) -> None:
    """Mirror streamed text into message_queue.partial_response (msgqueue.set_partial)."""
    if not message_ids or db is None:
        return
    try:
        placeholders = ",".join("?" * len(message_ids))
        async with db.execute(
            f"SELECT id FROM message_queue WHERE message_id IN ({placeholders})",
            tuple(message_ids),
        ) as cursor:
            ids = [r["id"] for r in await cursor.fetchall()]
        for row_id in ids:
            await msgqueue.set_partial(db, row_id, text)
    except Exception as e:
        log.warning(f"partial response write failed: {e}")


# agent -> {"fh": file object, "day": "YYYY-MM-DD", "bytes": int}. The handle
# is kept open across turns: this runs once per stream event, and reopening
# per line would put three syscalls on the hot path instead of one.
_stream_log_files: Dict[str, Dict] = {}


def _open_stream_log(agent: str):
    """Open a fresh stream log for `agent` and return (handle, day, path).

    The name must match the `{agent}_*.jsonl` glob the manual summarizer
    uses. Unbuffered binary append: the summarizer is a *separate*
    process reading this file, so a buffered write would leave it reading
    a stale tail.
    """
    STREAM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    # Second-resolution names are what an operator wants to read, but two
    # rotations can land in the same second — so a colliding name gets a
    # counter rather than silently reopening the file we just rolled off.
    path = STREAM_LOG_DIR / f"{agent}_{stamp}.jsonl"
    n = 1
    while path.exists():
        path = STREAM_LOG_DIR / f"{agent}_{stamp}-{n}.jsonl"
        n += 1
    return open(path, "ab", buffering=0), now.strftime("%Y-%m-%d"), path


def redact_for_log(text, limit: int = 200) -> str:
    """Mask credential-shaped strings, then truncate (a token must not be cut
    in half and escape the patterns). lib/redact.py owns the pattern list.

    Raw CLI output (a garbled stream line, an auth failure body) can carry
    tokens. This is a best-effort mask, not a guarantee.
    """
    return redact.redact_text(str(text if text is not None else ""))[:limit]


def write_stream_log(agent: str, line: bytes) -> None:
    """Tee one raw stream-json event to the agent's stream log.

    Best-effort by construction: this sits in the readline loop of every
    turn, and a full disk or a revoked permission must cost the log line,
    never the agent's reply. Nothing here raises.
    """
    if not line:
        return
    # The tee is masked; the event the loop parses and acts on is the original.
    # A redaction failure costs the line, never the reply.
    try:
        line = redact.redact_line(line)
    except Exception as e:
        log.debug(f"stream log redaction failed: {e}")
        return
    if not line.endswith(b"\n"):
        line = line + b"\n"

    # Two attempts: a handle that has gone bad (fd closed, file deleted under
    # us) is dropped and the same line retried against a fresh one, so one
    # bad handle costs at most one write rather than the rest of the session.
    for attempt in (0, 1):
        try:
            entry = _stream_log_files.get(agent)
            today = datetime.now().strftime("%Y-%m-%d")
            if entry is None or entry["day"] != today or entry["bytes"] >= STREAM_LOG_MAX_BYTES:
                if entry is not None:
                    try:
                        entry["fh"].close()
                    except Exception:
                        pass
                fh, day, path = _open_stream_log(agent)
                entry = {"fh": fh, "day": day, "bytes": 0}
                _stream_log_files[agent] = entry
                log.info(f"{agent} stream log: {path}")

            entry["fh"].write(line)
            entry["bytes"] += len(line)
            return
        except Exception as e:
            stale = _stream_log_files.pop(agent, None)
            if stale is not None:
                try:
                    stale["fh"].close()
                except Exception:
                    pass
            if attempt:
                log.debug(f"Could not tee stream log for {agent}: {e}")


def extract_permission_denials(result_event: Dict) -> List[Dict]:
    """Pull the `permission_denials` list off a stream-json `result` event.
    Always a list, never None, so callers can iterate unconditionally."""
    return result_event.get("permission_denials") or []


async def read_agent_response(
    agent: str, channel_id: str, message_ids: Optional[List[str]] = None
) -> tuple[str, Dict]:
    """Read and process agent response stream"""
    return await turn_loop.read_events(STATE, agent, channel_id, message_ids)


# =============================================================================
# Usage-limit wall: hold the batch, don't consume it
# =============================================================================

# When a turn hits the Claude usage wall the `result` event's error text used
# to become the reply: it was posted to the channel and the batch marked
# COMPLETE, so the human's message was consumed and nothing replayed it once
# the window reset. Now the batch goes back to STATUS_QUEUED with a
# `not_before` time, one short notice is posted, and a wake timer replays it.
# Ported from the household's usage-wall hold (59caa0c2b, 2bcc9c110,
# 39953b5c9, f6b5baaaf) and rate-limit breaker (2548524f9), minus the
# tmux/PTY pane mechanics.

GENERIC_TURN_ERROR = "The agent hit an error and the turn did not complete."

WALL_USAGE = "usage"
WALL_MODEL = "model"

# Context overflow is a different failure with a different cure (compaction /
# reset); never treat it as a wall even if the text mentions "limit".
CONTEXT_OVERFLOW_RE = re.compile(
    r"prompt is too long|context (window|length|limit)|maximum context|too many tokens",
    re.IGNORECASE)
USAGE_WALL_RE = re.compile(
    r"hit your (\w+ )?limit|(session|weekly|usage|opus|sonnet) limit"
    r"|usage limit reached|limit reached\b.*\bresets?|5-hour limit",
    re.IGNORECASE)
MODEL_WALL_RE = re.compile(
    r"issue with the selected model|model .{0,40}(may not exist|not available|unavailable)"
    r"|do(es)? not have access to .{0,20}model|model_not_found",
    re.IGNORECASE)

WALL_BACKOFF_BASE_SECONDS = 300
WALL_BACKOFF_MAX_SECONDS = 3600
WALL_MIN_HOLD_SECONDS = 60
WALL_RESET_MARGIN_SECONDS = 30

agent_wall_strikes: Dict[str, int] = {}
agent_hold_tasks: Dict[str, Any] = {}
agent_hold_notice_until: Dict[str, int] = {}


def classify_wall(text, is_error=False, rate_limit_rejected=None):
    """Return WALL_USAGE / WALL_MODEL / None for a finished turn.

    Only an errored turn can be a wall: a successful reply that merely
    mentions "session limit" must not be held. A `rejected` rate_limit_event
    on an errored turn counts as a usage wall even if the wording is new.
    """
    if not is_error:
        return None
    text = text or ""
    if CONTEXT_OVERFLOW_RE.search(text):
        return None
    if MODEL_WALL_RE.search(text):
        return WALL_MODEL
    if USAGE_WALL_RE.search(text):
        return WALL_USAGE
    if isinstance(rate_limit_rejected, dict):
        return WALL_USAGE
    return None


def wall_not_before(kind, text, rate_limit_rejected, strikes, now=None):
    """Epoch second before which the held batch must not be replayed.

    Prefers the CLI's own `resetsAt`, then an epoch in the error text
    ("usage limit reached|1760000000"). A reset that is missing or already
    past (it would hot-loop against the wall) falls back to exponential
    backoff keyed on consecutive strikes.
    """
    now = int(time.time() if now is None else now)
    reset = None
    if kind == WALL_USAGE:
        if isinstance(rate_limit_rejected, dict):
            r = rate_limit_rejected.get("resetsAt")
            if isinstance(r, (int, float)):
                reset = int(r)
        if reset is None:
            m = re.search(r"\|(\d{10})\b", text or "")
            if m:
                reset = int(m.group(1))
    if reset is not None and reset > now:
        return reset + WALL_RESET_MARGIN_SECONDS
    delay = min(WALL_BACKOFF_BASE_SECONDS * (2 ** max(strikes, 0)), WALL_BACKOFF_MAX_SECONDS)
    return now + max(delay, WALL_MIN_HOLD_SECONDS)


def format_wall_notice(kind, until):
    when = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M UTC")
    what = "model unavailable" if kind == WALL_MODEL else "usage limit reached"
    return f"⏸️ {what} — your message is held and will be processed after {when}."


async def agent_hold_until(agent: str, now=None):
    """Latest future `not_before` among the agent's queued rows, or None."""
    now = int(time.time() if now is None else now)
    # execute_fetchall: one hop that also finalizes the statement. An open
    # cursor (execute() then fetchone()) keeps SQLite's SHARED lock across the
    # next await, which blocks any other writer on the file.
    rows = await db.execute_fetchall(
        "SELECT MAX(not_before) AS nb FROM message_queue"
        " WHERE agent = ? AND processed = ? AND not_before > ?",
        (agent, STATUS_QUEUED, now),
    )
    return rows[0]["nb"] if rows and rows[0]["nb"] else None


def schedule_hold_wake(agent: str, until: int) -> None:
    """Arm (or re-arm) the single timer that replays a held agent's queue."""
    existing = agent_hold_tasks.get(agent)
    if existing and not existing.done():
        if getattr(existing, "hold_until", None) == until:
            return
        existing.cancel()

    async def wake():
        await asyncio.sleep(max(until - time.time(), 0) + 1)
        agent_hold_tasks.pop(agent, None)
        agent_hold_notice_until.pop(agent, None)
        await process_agent_queue(agent)

    task = asyncio.create_task(wake())
    task.hold_until = until
    agent_hold_tasks[agent] = task


async def hold_batch(agent, channel_id, message_ids, kind, until):
    """Put a batch back on the queue until `until`; notice at most once."""
    await db.execute(
        f"""
        UPDATE message_queue
        SET processed = ?, not_before = ?, processing_started_at = NULL,
            claimed_by = NULL
        WHERE message_id IN ({','.join('?' * len(message_ids))})
        """,
        (STATUS_QUEUED, until, *message_ids),
    )
    await db.commit()
    log.warning(f"{agent} hit a {kind} wall; holding {len(message_ids)} message(s) until {until}")
    if channel_id != "0" and agent_hold_notice_until.get(agent) != until:
        agent_hold_notice_until[agent] = until
        await post_to_discord(agent, channel_id, format_wall_notice(kind, until))
    schedule_hold_wake(agent, until)

async def process_agent_queue(agent: str):
    """Process pending messages for agent"""
    await turn_loop.drain_shard(STATE, agent)

# =============================================================================
# Crash Recovery
# =============================================================================

async def crash_recovery():
    """Recover from crashes on startup"""
    # Find messages stuck in PROCESSING state
    async with db.execute(
        "SELECT * FROM message_queue WHERE processed = ?",
        (STATUS_IN_PROGRESS,)
    ) as cursor:
        stuck_messages = await cursor.fetchall()

    if stuck_messages:
        log.warning(f"Found {len(stuck_messages)} stuck messages from previous crash")

        for msg in stuck_messages:
            # Mark as crashed
            await db.execute(
                "UPDATE message_queue SET processed = ? WHERE message_id = ?",
                (STATUS_CRASHED, msg["message_id"])
            )

            # Notify channel
            channel_id = msg["channel_id"]
            agent = msg["agent"]
            if channel_id != "0":
                crash_msg = f"⚠️ {agent} crashed while processing message from {msg['author']}"
                await post_to_discord(agent, channel_id, crash_msg)

        await db.commit()

    # Retry posting messages that completed but weren't posted. Only recent
    # rows (24 h): an older unposted row is logged once and left alone rather
    # than reposted on every boot. PASS and blank rows are never posted, and a
    # reply the outbox already owns or delivered is never posted a second time.
    async with db.execute(
        "SELECT * FROM message_queue WHERE processed = ? AND discord_response_id IS NULL "
        "AND channel_id != '0' AND processed_at >= datetime('now', '-24 hours')",
        (STATUS_COMPLETE,)
    ) as cursor:
        unposted = await cursor.fetchall()
    async with db.execute(
        "SELECT COUNT(*) FROM message_queue WHERE processed = ? AND discord_response_id IS NULL "
        "AND channel_id != '0' AND response IS NOT NULL AND response != '' "
        "AND (processed_at IS NULL OR processed_at < datetime('now', '-24 hours'))",
        (STATUS_COMPLETE,)
    ) as cursor:
        stale = (await cursor.fetchone())[0]
    if stale:
        log.info(f"Leaving {stale} unposted response(s) older than 24h alone")

    if unposted:
        log.warning(f"Found {len(unposted)} unposted responses, retrying")
        for msg in unposted:
            if not msg["response"] or not post_guard.post_decision(msg["response"])[0]:
                continue
            if (discord_ux.parse_react_reply(msg["response"])
                    and ux_for_channel_id(msg["channel_id"]).reactions):
                continue  # a reaction, never text
            poster = STATE.agent_of(msg["agent"])
            matched = None
            conn = _outbox_store()
            if conn is not None:
                try:
                    sha = outbox_lib.content_sha(msg["response"])
                    processed = datetime.strptime(
                        msg["processed_at"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc).timestamp()
                    # Stable key first: a row enqueued on any earlier boot,
                    # still pending or delivered later by the loop, is found
                    # however long ago. Content/time is only the fallback for
                    # rows without a key.
                    matched = outbox_lib.find_by_queue_id(conn, msg["message_id"])
                    if matched is None:
                        matched = outbox_lib.find_for_reply(
                            conn, poster, msg["channel_id"], sha, processed)
                    if matched is None:
                        # Fallback for keyless rows enqueued by this sweep.
                        matched = outbox_lib.find_for_reply(
                            conn, poster, msg["channel_id"], sha, _outbox_now())
                except Exception as e:
                    _outbox_broken("sweep lookup", e)
            if matched is not None:
                if matched["status"] != "delivered":
                    continue  # the outbox owns delivery; dead rows are the operator's
                ids = json.loads(matched.get("message_ids") or "[]")
                discord_id = ids[-1] if ids else None
            else:
                # Through the outbox (durable), so the reply survives another
                # outage instead of being reposted on every boot.
                discord_id = await post_to_discord(poster, msg["channel_id"], msg["response"],
                                                   dead_letter=True,
                                                   queue_message_id=msg["message_id"])
            if discord_id:
                # Commit per-message, not once after the whole loop. The
                # record of delivery (discord_response_id written) and the
                # delivery itself (post_to_discord succeeding) need to be
                # atomic with each other, not just with the DB. A batched
                # commit after the loop means a crash partway through
                # leaves every already-posted-but-not-yet-committed
                # message's discord_response_id at NULL, so the *next*
                # crash_recovery() sweep finds and reposts them — the
                # recovery path duplicating exactly what it exists to
                # prevent. Committing immediately after each successful
                # post bounds the risk to the single message in flight at
                # crash time, not the whole batch.
                await db.execute(
                    "UPDATE message_queue SET discord_response_id = ? WHERE message_id = ?",
                    (discord_id, msg["message_id"])
                )
                await db.commit()

# =============================================================================
# HTTP API
# =============================================================================

async def handle_message(request):
    """POST /message - Queue message for agent"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()

    agent = data.get("agent")
    shard_hint = data.get("shard")
    channel = data.get("channel", "general")
    channel_id = data.get("channel_id", "0")
    server = data.get("server", "discord")
    author = data.get("author", "unknown")
    author_id = data.get("author_id", "0")
    is_bot = data.get("is_bot", False)
    content = data.get("content", "")
    message_id = data.get("message_id", f"msg-{uuid.uuid4()}")
    mentions_agent = data.get("mentions_agent", False)
    attachments = data.get("attachments") or []
    if not isinstance(attachments, list):
        return web.json_response({"error": "attachments must be a list"}, status=400)
    thread_parent_id = data.get("thread_parent_id")
    if thread_parent_id:
        ux_threads[str(channel_id)] = str(thread_parent_id)

    # The relay sends the shard it routed to (2.2). An agent id alone selects
    # the agent's first shard. An unknown shard with a valid agent falls back to
    # that agent's first shard (the relay's config can lag a registry reload).
    specs = effective_specs()
    owners = {sp.id: sp.agent for sp in specs}
    claimed_agent = agent
    if shard_hint and shard_hint in owners:
        agent = shard_hint
        if claimed_agent and claimed_agent in owners.values() \
                and owners[shard_hint] != claimed_agent:
            log.warning(f"message shard {shard_hint!r} belongs to agent "
                        f"{owners[shard_hint]!r}, not {claimed_agent!r}; using the shard")
    else:
        agent = shards_lib.first_shard(specs, claimed_agent) if claimed_agent else None
        if shard_hint and agent:
            log.warning(f"message for unknown shard {shard_hint}, using {agent}")
    if not agent:
        return web.json_response({"error": "Invalid agent"}, status=400)

    # An image posted with no caption is a real message with empty text. It
    # used to be rejected here as "Empty content", which is the first place
    # attachment support has to stop failing.
    if not content and not attachments:
        return web.json_response({"error": "Empty content"}, status=400)

    # Check cost limits (unless owner or heartbeat)
    if server != "local" and author_id != OWNER_DISCORD_ID:
        cost_check = await check_cost_limits(author_id)
        if cost_check["exceeded"]:
            return web.json_response(
                {"error": "Cost limit exceeded", "reason": cost_check["reason"]},
                status=429,
                headers={"Retry-After": "3600"}
            )

    # Check queue depth
    async with db.execute(
        "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
        (agent, STATUS_QUEUED)
    ) as cursor:
        row = await cursor.fetchone()
        if row["count"] >= QUEUE_DEPTH_LIMIT:
            return web.json_response({"error": "Queue full"}, status=503)

    # Insert message
    try:
        await db.execute(
            """
            INSERT INTO message_queue
            (agent, channel, channel_id, server, author, author_id, is_bot, content, message_id, mentions_agent, attachments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent, channel, channel_id, server, author, author_id, int(is_bot), content, message_id,
             int(mentions_agent), json.dumps(attachments) if attachments else None)
        )
        await db.commit()
        msgqueue.notify(agent)
    except aiosqlite.IntegrityError:
        # A message_id we already queued: the deferred-message flusher (#88)
        # re-firing a payload whose first POST landed but whose response was
        # lost, or any client retrying blind. The first copy is being handled;
        # accepting the duplicate is the idempotent answer, and anything
        # non-2xx here would make the flusher retry it until stale-out.
        return web.json_response(
            {"status": "duplicate", "message_id": message_id}, status=202
        )
    except Exception as e:
        log.error(f"Error inserting message: {e}")
        return web.json_response({"error": "Database error"}, status=500)

    # Trigger processing if agent is idle, or show typing if it is mid-turn.
    turn_loop.notify_enqueued(STATE, agent, channel_id)

    return web.json_response({"status": "queued", "message_id": message_id}, status=202)


async def handle_message_edit(request):
    """POST /message/edit - A person edited a message the relay already sent.

    Decided by discord_ux.edit_decision: update a still-queued row in place, or
    queue a follow-up row (same agent column, so the same shard) telling the
    agent what changed. Additive; /message is untouched."""
    if not _authorized(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    server = data.get("server", "discord")
    message_id = str(data.get("message_id") or "")
    author_id = str(data.get("author_id") or "")
    content = data.get("content")
    if not message_id or not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "message_id and content required"}, status=400)

    async def load():
        async with db.execute(
            "SELECT *, CAST(strftime('%s', created_at) AS INTEGER) AS created_at_ts"
            " FROM message_queue WHERE server = ? AND message_id = ?",
            (server, message_id)
        ) as cur:
            r = await cur.fetchone()
        prefix = f"edit:{message_id}:"
        async with db.execute(
            "SELECT message_id, processed, content FROM message_queue"
            " WHERE server = ? AND substr(message_id, 1, ?) = ? ORDER BY id",
            (server, len(prefix), prefix)
        ) as cur:
            f = [dict(x) for x in await cur.fetchall()]
        return (dict(r) if r else None), f

    row, followups = await load()
    if row is None:
        return web.json_response({"status": "unknown", "message_id": message_id})
    # A thread row is labelled "parent/thread"; the parent's config governs it.
    cfg = ux_config().for_channel((row["channel"] or "").split("/", 1)[0]).edit_reroute
    if cfg is None:
        return web.json_response({"status": "disabled", "message_id": message_id})

    decision = None
    for _ in range(2):
        decision = discord_ux.edit_decision(
            row, _ux_now(), cfg.window_s, cfg.max_followups, followups,
            author_id=author_id, new_text=content)
        if decision.action != "update":
            break
        cur = await db.execute(
            "UPDATE message_queue SET content = ? WHERE message_id = ? AND processed = 0",
            (content, message_id))
        await db.commit()
        if cur.rowcount:
            return web.json_response({"status": "updated", "message_id": message_id})
        # The claim won the race: decide again against the claimed row.
        row, followups = await load()
    if decision.action == "update":
        return web.json_response({"status": "unchanged", "message_id": message_id})

    if decision.action in ("followup", "update_followup"):
        text = discord_ux.edit_followup_text(
            row["author"], row["content"], content, decision.phase)
        if decision.action == "update_followup":
            cur = await db.execute(
                "UPDATE message_queue SET content = ? WHERE message_id = ? AND processed = 0",
                (text, decision.followup_id))
            await db.commit()
            if cur.rowcount:
                return web.json_response({"status": "updated", "message_id": message_id})
            # Claimed meanwhile: it is now a turn in flight; fall through to a new row
            # unless that would exceed the cap.
            if len(followups) >= cfg.max_followups:
                return web.json_response({"status": "refused", "message_id": message_id})
        n = len(followups) + 1
        agent = row["agent"]
        try:
            await db.execute(
                """
                INSERT INTO message_queue
                (agent, channel, channel_id, server, author, author_id, is_bot, content,
                 message_id, mentions_agent, priority)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0)
                """,
                (agent, row["channel"], row["channel_id"], server, row["author"],
                 row["author_id"], text, f"edit:{message_id}:{n}",
                 int(row["mentions_agent"] or 0)))
            await db.commit()
        except aiosqlite.IntegrityError:
            return web.json_response({"status": "duplicate", "message_id": message_id})
        msgqueue.notify(agent)
        turn_loop.notify_enqueued(STATE, agent, row["channel_id"])
        return web.json_response({"status": "followup", "message_id": message_id,
                                  "followup_id": f"edit:{message_id}:{n}"})

    return web.json_response({"status": decision.status, "message_id": message_id})


def outbox_health() -> Dict[str, Any]:
    zero = {"pending": 0, "sending": 0, "dead": 0, "oldest_pending_age_s": None}
    conn = _outbox_store(create=False)
    if conn is None:
        return zero
    try:
        return outbox_lib.stats(conn, _outbox_now())
    except Exception as e:
        _outbox_broken("stats", e)
        return zero


def _authorized(request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    return auth_header.startswith("Bearer ") and auth_header[7:] == AGENT_SERVER_TOKEN


async def handle_outbox_list(request):
    """GET /outbox?status=&limit= - rows without content (content only for
    status=dead with include_content=1)."""
    if not _authorized(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    conn = _outbox_store(create=False)
    if conn is None:
        return web.json_response({"rows": []})
    q = request.query
    try:
        limit = int(q.get("limit", "50"))
    except ValueError:
        limit = 50
    rows = outbox_lib.list_rows(conn, q.get("status") or None, limit,
                                include_content=(q.get("status") == "dead"
                                                 and q.get("include_content") == "1"))
    return web.json_response({"rows": rows})


async def handle_outbox_get(request):
    """GET /outbox/{id} - the row (no content) and its audit events."""
    if not _authorized(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    conn = _outbox_store(create=False)
    row = outbox_lib.show(conn, request.match_info["id"]) if conn is not None else None
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(row)


async def _outbox_verb(request, fn):
    if not _authorized(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    conn = _outbox_store(create=False)
    rid = request.match_info["id"]
    if conn is None or outbox_lib.get(conn, rid) is None:
        return web.json_response({"error": "not found"}, status=404)
    changed = fn(conn, rid, _outbox_now())
    if changed:
        _outbox_notify()
    row = outbox_lib.get(conn, rid)
    return web.json_response({"id": rid, "changed": changed, "status": row["status"]},
                             status=200 if changed else 409)


async def handle_outbox_retry(request):
    """POST /outbox/{id}/retry - dead or discarded back to pending, attempts reset."""
    return await _outbox_verb(request, outbox_lib.retry)


async def handle_outbox_discard(request):
    """POST /outbox/{id}/discard - stop delivering (and counting) a pending or dead row."""
    return await _outbox_verb(request, outbox_lib.discard)


async def handle_health(request):
    """GET /health - Health check.

    `dead_letters` is the number of `dead` outbox rows. `dead_letter_path` is
    DEPRECATED: it now names the outbox file; read `outbox` instead.
    """
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent_status = {}
    shard_status = {}
    ctx_by_shard = await get_context_tokens()
    specs = effective_specs()
    for sp in specs:
        proc = agent_processes.get(sp.id)
        async with db.execute(
            "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
            (sp.id, STATUS_QUEUED)
        ) as cursor:
            row = await cursor.fetchone()
            queue_depth = row["count"]
        shard_status[sp.id] = {
            "state": agent_states.get(sp.id, "UNKNOWN"),
            "alive": proc is not None and proc.returncode is None,
            "queue_depth": queue_depth,
            "session_id": agent_sessions.get(sp.id, "")[:8],
            # 0 = unknown.
            "context_tokens": ctx_by_shard.get(sp.id, 0),
            "stolen_total": STATE.stolen_total.get(sp.id, 0),
        }

    for agent in dict.fromkeys(sp.agent for sp in specs):
        mine = [sp.id for sp in specs if sp.agent == agent]
        agent_status[agent] = {
            "state": shards_lib.aggregate_state(shard_status[i]["state"] for i in mine),
            "alive": any(shard_status[i]["alive"] for i in mine),
            "queue_depth": sum(shard_status[i]["queue_depth"] for i in mine),
            "session_id": shard_status[mine[0]]["session_id"],
            "context_tokens": max(shard_status[i]["context_tokens"] for i in mine),
            # Per-shard values; keyed by shard id.
            "shards": {i: shard_status[i] for i in mine},
        }

    # A non-zero count means replies were generated and gave up (outbox `dead`
    # rows). It is reported here because that failure is otherwise invisible — every agent
    # looks idle and healthy while its answers are going nowhere.
    undelivered = dead_letter_count()

    return web.json_response({
        "status": "healthy",
        # Both of these are read by the dashboard home page's summary cards,
        # which showed a hardcoded 0 for as long as nothing served them.
        # queue_depth is the sum of what the loop above already counted.
        "uptime_seconds": int(time.time() - SERVER_START_TS),
        "queue_depth": sum(a["queue_depth"] for a in agent_status.values()),
        "agents": agent_status,
        "shards": shard_status,
        "dead_letters": undelivered,
        # Deprecated: now the outbox file; kept so existing readers do not break.
        "dead_letter_path": str(OUTBOX_PATH),
        "outbox": outbox_health(),
    })

def model_info(agent):
    """(effective model, source) for an agent; every shard shares it."""
    return runtime_overrides.effective_model(WORKSPACE_ROOT, agent, agent_config.get(agent, {}))


def effort_info(agent):
    """(effective level or None, source) for an agent; every shard shares it."""
    return runtime_overrides.effective_effort(WORKSPACE_ROOT, agent, agent_config.get(agent, {}))


def _paused_entry(shard):
    """null, or {reason, until} while this shard is held: an operator pause
    ("manual") first, then the usage gate. The one read of the paused view."""
    return operator_pause.paused_view(STATE, shard)


async def handle_agents(request):
    """GET /agents - List agents"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agents_list = []
    ctx_by_shard = await get_context_tokens()
    specs = effective_specs()
    async with db.execute(
        "SELECT agent, COUNT(*) AS count FROM message_queue WHERE processed = ? GROUP BY agent",
        (STATUS_QUEUED,)
    ) as cursor:
        depth_by_shard = {r["agent"]: r["count"] for r in await cursor.fetchall()}
    for agent in dict.fromkeys(sp.agent for sp in specs):
        config = agent_config.get(agent, {})
        mine = [sp for sp in specs if sp.agent == agent]
        shard_rows = []
        for sp in mine:
            proc = agent_processes.get(sp.id)
            shard_rows.append({
                "id": sp.id,
                "is_default": sp.is_default,
                "state": agent_states.get(sp.id, "UNKNOWN"),
                "alive": proc is not None and proc.returncode is None,
                "pid": proc.pid if proc is not None else None,
                "session_id": agent_sessions.get(sp.id, "")[:8],
                "queue_depth": depth_by_shard.get(sp.id, 0),
                "context_tokens": ctx_by_shard.get(sp.id, 0),
                "channels": list(sp.channels),
                "last_channel": agent_last_channel.get(sp.id),
                "paused": _paused_entry(sp.id),
                "effort": effort_info(sp.agent)[0],
                "effort_source": effort_info(sp.agent)[1],
                "stolen_total": STATE.stolen_total.get(sp.id, 0),
                "steer_pending": len(STATE.steer[sp.id].entries) if sp.id in STATE.steer else 0,
                "steered_total": STATE.steered_total.get(sp.id, 0),
            })
        agents_list.append({
            "name": agent,
            # Max over the agent's shards; 0 = unknown. Per-shard values in `shards`.
            "context_tokens": max(r["context_tokens"] for r in shard_rows),
            "shards": shard_rows,
            "model": model_info(agent)[0],
            "model_source": model_info(agent)[1],
            # The same defaults the subprocess is actually launched with (see
            # start_agent). Reporting the raw config.get() would show a blank
            # for every agent that relies on the default, which reads as "not
            # configured" rather than "configured by omission".
            "max_turns": config.get("max_turns", 200),
            "timeout": config.get("timeout"),
            "state": shards_lib.aggregate_state(r["state"] for r in shard_rows),
            "has_discord_token": agent in AGENT_TOKENS,
            # Chat-picker hygiene: not every configured agent is meant to be
            # talked to directly from the dashboard (a low-capability relay
            # exists to route, not converse). Defaults true so existing
            # agents.yaml files with no opinion keep showing up. "label" is
            # a human-friendly display name, defaulting to the raw agent key.
            "dashboard_chat": config.get("dashboard_chat", True),
            "label": config.get("label", agent),
        })

    return web.json_response({"agents": agents_list})

def _resolve_request_targets(request) -> List[str]:
    """Shard ids the `/agents/{name}/...` path (and optional ?shard=) means."""
    return shards_lib.resolve_targets(
        effective_specs(), request.match_info.get("name"),
        request.rel_url.query.get("shard"))


def _with_shards(body: Dict[str, Any], targets: List[str]) -> Dict[str, Any]:
    if len(targets) > 1:
        body["shards"] = list(targets)
    return body


async def sync_shards(new_specs, old_specs=None) -> Dict[str, List[str]]:
    """Make the running shards match `new_specs` (after a registry reload).

    Added shards get lock/state/buffer/beacon and a subprocess; removed shards
    are killed, forgotten, and their still-QUEUED rows marked SKIPPED
    ('shard removed'); their sessions and cost_events rows are left alone. Kept
    shards are not touched, so their PIDs do not change.
    """
    old = list(old_specs) if old_specs is not None else list(effective_specs())
    _set_shard_specs(new_specs)
    _rebuild_token_maps()
    diff = shards_lib.diff_shards(old, new_specs)

    for sp in diff.removed:
        sid = sp.id
        await kill_agent_subprocess(sid)
        await turn_loop.release_pending(STATE, sid, "shard removed")
        for d in (STATE.steer, STATE.steer_lock, STATE.steer_unmatched, STATE.steered_total,
                  STATE.turn_seq, STATE.enqueued_at, STATE.pushback, STATE.bg_seen,
                  STATE.replay_seen):
            d.pop(sid, None)
        for d in (agent_locks, agent_states, response_buffers, agent_last_cost,
                  agent_sessions, agent_last_channel, agent_turn_context,
                  agent_wall_strikes, respawn_history, _last_beacon_write):
            d.pop(sid, None)
        task = agent_hold_tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()
        agent_hold_notice_until.pop(sid, None)
        deliberate_kills.discard(sid)
        interrupted_agents.discard(sid)
        await db.execute(
            "UPDATE message_queue SET processed = ?, response = ? "
            "WHERE agent = ? AND processed = ?",
            (STATUS_SKIPPED, "shard removed", sid, STATUS_QUEUED))
        await db.commit()
        try:
            (AGENT_BEACON_DIR / f"{sid}.json").unlink()
        except OSError:
            pass
        log.info(f"Shard {sid} removed")

    for sp in diff.added:
        agent_locks[sp.id] = asyncio.Lock()
        agent_states[sp.id] = "IDLE"
        response_buffers[sp.id] = ""
        write_agent_beacon(sp.id, "IDLE", force=True)
    for sp in diff.added:
        log.info(f"Shard {shards_lib.shard_label(sp)} added")
        await start_agent_subprocess(sp.id)

    return {"added": [s.id for s in diff.added],
            "removed": [s.id for s in diff.removed]}


async def handle_agent_reset(request):
    """POST /agents/{name}/reset - Reset agent session"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    # Operator resets are cold (no handoff) unless ?handoff=1 asks for the
    # handoff turn first and the shard's effective handoff_on_reset allows it.
    # The handoff turn can take minutes, so that path is scheduled, not awaited.
    want_handoff = request.rel_url.query.get("handoff") in ("1", "true")
    scheduled = []
    for t in targets:
        if want_handoff and STATE.cfg(t).get("handoff_on_reset"):
            scheduled.append(t)
            asyncio.create_task(begin_reset(t, "operator"))
        else:
            await restart_agent(t)
    if scheduled:
        body = {"status": "scheduled", "handoff": scheduled}
        return web.json_response(_with_shards(body, targets))
    return web.json_response(_with_shards({"status": "reset"}, targets))


async def handle_session_finalize(request):
    """POST /agents/{name}/session/finalize - the agent wants a fresh session.

    Schedules a reset (with a handoff note) for the end of the shard's current
    turn; the `session` MCP tool calls it with the caller's shard id."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    shard = shards_lib.first_shard(effective_specs(), request.match_info.get("name") or "")
    if not shard:
        return web.json_response({"error": "Unknown agent"}, status=404)
    STATE.session_policy.reset_requested[shard] = sp_lib.REASON_AGENT
    log.info(f"{shard} requested a fresh session; reset at the end of this turn")
    return web.json_response({"status": "scheduled", "shard": shard})


async def handle_agent_reload(request):
    """POST /agents/{name}/reload - Bounce subprocess, preserve session."""
    global agent_config
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    # B19: re-read the registry so edits to agents.yaml take effect. An invalid
    # edit keeps the running config and reports every problem.
    try:
        reg = agent_registry.load_registry(WORKSPACE_ROOT)
    except agent_registry.RegistryError as e:
        return web.json_response(
            {"error": "invalid agents.yaml; keeping previous config",
             "problems": e.problems},
            status=400,
        )
    agent_config = reg.legacy_view()["agents"]
    reload_channels_config()
    changes = await sync_shards(shards_lib.plan_shards(reg))
    if changes["added"] or changes["removed"]:
        # A shard-topology change is applied on its own: kept shards are not
        # bounced (their PIDs stay), and the response says what changed.
        return web.json_response({"status": "reloaded", **changes})

    targets = [t for t in targets if t in agent_processes or t in agent_states]
    for t in targets:
        await reload_agent(t)
    return web.json_response(_with_shards({"status": "reloaded"}, targets))


async def handle_agent_interrupt(request):
    """POST /agents/{name}/interrupt - Stop the current generation."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    # Optional body {"message", "channel_id", "author"} (2.5): interrupt and
    # follow with a message that runs first. No body keeps today's behaviour.
    body_in = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            body_in = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body_in = {}
    message = body_in.get("message")
    if isinstance(message, str) and message.strip():
        if len(targets) > 1:
            return web.json_response({"error": "shard required"}, status=400)
        out = await turn_loop.interrupt_with_message(
            STATE, targets[0], message,
            channel_id=str(body_in.get("channel_id") or "0"),
            author=str(body_in.get("author") or "interrupt"),
            fallback=interrupt_agent)
        return web.json_response({
            "status": "interrupted" if out["interrupted"] else "queued",
            "interrupted": out["interrupted"],
            "message_id": out["message_id"],
            "mode": out["mode"],
        })

    # interrupt_agent is a no-op for a shard that is not PROCESSING, so an
    # agent id interrupts only the busy ones.
    hit = [t for t in targets if await interrupt_agent(t)]
    interrupted = bool(hit)
    # 200 either way: "it was already idle" is a successful answer to
    # "stop what you are doing", and the relay says which one happened.
    body = {
        "status": "interrupted" if interrupted else "idle",
        "interrupted": interrupted,
    }
    if len(targets) > 1:
        body["shards"] = hit
    return web.json_response(body)


# Shards whose effort change waits for the turn in flight (spec 6.4).
effort_pending: set = set()
effort_respawning: set = set()


def effort_on_turn_end(shard, result):
    """A PROCESSING shard's effort change lands after its turn: the reply has
    posted, new claims are held, then the subprocess respawns (session kept)."""
    if shard not in effort_pending:
        return
    effort_pending.discard(shard)
    effort_respawning.add(shard)

    async def respawn():
        try:
            await reload_agent(shard)
        finally:
            effort_respawning.discard(shard)
        await turn_loop.drain_shard(STATE, shard)

    result.followups.append(respawn)


def effort_before_claim(shard):
    return "effort-respawn" if shard in effort_respawning else None


async def handle_agent_effort(request):
    """POST /agents/{name}/effort {"level": low|medium|high|xhigh|max|default}.
    Agent-level: the owning agent of {name} (every shard) takes the level."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)
    body_in = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            body_in = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body_in = {}
    level = body_in.get("level")
    valid = agent_registry.EFFORTS + (runtime_overrides.DEFAULT,)
    if level not in valid:
        return web.json_response(
            {"error": f"level must be one of {', '.join(valid)}"}, status=400)
    if level != runtime_overrides.DEFAULT and not await asyncio.to_thread(
            runtime_overrides.cli_supports_effort):
        return web.json_response(
            {"error": "effort is not supported by this CLI version"}, status=400)
    agent = STATE.agent_of(targets[0])
    runtime_overrides.set_effort(WORKSPACE_ROOT, agent, level)
    shards = [sp.id for sp in effective_specs() if sp.agent == agent]
    applied, deferred = [], []
    for sh in shards:
        if sh in agent_processes and agent_states.get(sh) == "PROCESSING":
            effort_pending.add(sh)
            deferred.append(sh)
        elif sh in agent_processes:
            await reload_agent(sh)
            applied.append(sh)
        else:
            applied.append(sh)      # nothing running: the next spawn reads it
    return web.json_response({"effort": effort_info(agent)[0],
                              "applied": applied, "deferred": deferred})


async def handle_agent_model(request):
    """POST /agents/{name}/model {"model": "<allowed id>"|"default"}.
    Agent-level like effort: every shard takes it, at once when idle, after the
    turn in flight otherwise. Allowed ids come from config/models.yaml."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)
    body_in = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            body_in = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body_in = {}
    model = body_in.get("model")
    agent = STATE.agent_of(targets[0])
    if not isinstance(model, str) or not model:
        return web.json_response({"error": "model is required"}, status=400)
    try:
        runtime_overrides.set_model(WORKSPACE_ROOT, agent, model, agent_config.get(agent, {}))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    shards = [sp.id for sp in effective_specs() if sp.agent == agent]
    applied, deferred = [], []
    for sh in shards:
        if sh in agent_processes and agent_states.get(sh) == "PROCESSING":
            effort_pending.add(sh)      # same respawn-after-turn path as effort
            deferred.append(sh)
        elif sh in agent_processes:
            await reload_agent(sh)
            applied.append(sh)
        else:
            applied.append(sh)
    now, src = model_info(agent)
    return web.json_response({"model": now, "source": src,
                              "applied": applied, "deferred": deferred})


async def handle_agent_pause(request):
    """POST /agents/{name}/pause - hold the queue; the turn in progress finishes.
    Body {"minutes": 1..1440|null, "by": str|null}."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)
    body_in = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            body_in = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body_in = {}
    minutes = body_in.get("minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)
                                or not (operator_pause.MIN_MINUTES <= minutes
                                        <= operator_pause.MAX_MINUTES)):
        return web.json_response(
            {"error": f"minutes must be {operator_pause.MIN_MINUTES} to "
                      f"{operator_pause.MAX_MINUTES} or null"}, status=400)
    by = body_in.get("by")
    until = await operator_pause.pause(
        STATE, targets, minutes, by if isinstance(by, str) else None)
    return web.json_response({"paused": targets, "until": until})


async def handle_agent_resume(request):
    """POST /agents/{name}/resume - release an operator pause and drain."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)
    gone = await operator_pause.resume(STATE, targets)
    return web.json_response({"resumed": gone})


async def handle_agent_kill(request):
    """POST /agents/{name}/kill - Kill the subprocess without respawning it."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    was_running = any(t in agent_processes for t in targets)
    for t in targets:
        await kill_agent_subprocess(t)
    return web.json_response(
        _with_shards({"status": "killed", "was_running": was_running}, targets))


async def handle_agent_flush(request):
    """POST /agents/{name}/flush - Drop the agent's pending message queue."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    flushed = 0
    for t in targets:
        flushed += await flush_agent_queue(t)
    return web.json_response(
        _with_shards({"status": "flushed", "flushed": flushed}, targets))


# How much of a queued message body /agents/{name}/queue returns. The
# dashboard shows one truncated line per row, so shipping the full text of a
# long backlog would be megabytes of JSON nobody renders.
QUEUE_PREVIEW_CHARS = 200


async def handle_agent_queue(request):
    """GET /agents/{name}/queue - what this agent still has to answer.

    Read side of the dashboard's agent modal. Composed from message_queue —
    the same table /health already counts for queue_depth — rather than a new
    store, so there is exactly one source of truth for "what is waiting".

    Returns both STATUS_QUEUED and STATUS_IN_PROGRESS rows: the modal
    distinguishes them as "pending" vs "processing", and hiding the in-flight
    message would make an agent mid-turn look idle.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    # One shard keeps the dispatch order (priority first); several shards are
    # merged by arrival.
    order = ("priority DESC, created_at ASC, id ASC" if len(targets) == 1
             else "created_at ASC, id ASC")
    marks = ",".join("?" * len(targets))
    async with db.execute(
        "SELECT id, agent, channel, author, content, created_at, processed,"
        " call_id, reply_to_agent, priority, expires_at, depth,"
        " partial_response, restart_count, claimed_by, owner_agent"
        f" FROM message_queue WHERE agent IN ({marks}) AND processed IN (?, ?)"
        f" ORDER BY {order}",
        (*targets, STATUS_QUEUED, STATUS_IN_PROGRESS),
    ) as cursor:
        rows = await cursor.fetchall()

    messages = []
    for row in rows:
        content = row["content"] or ""
        messages.append({
            # The row's primary key, which is what DELETE below takes. Not
            # the `message_id` column — that is the upstream (Discord) id.
            "id": row["id"],
            "agent": row["agent"],
            "channel": row["channel"],
            "author": row["author"],
            "content": content[:QUEUE_PREVIEW_CHARS],
            "content_full_length": len(content),
            "created_at": row["created_at"],
            "state": (
                "processing" if row["processed"] == STATUS_IN_PROGRESS else "pending"
            ),
            **{k: row[k] for k in (
                "call_id", "reply_to_agent", "priority", "expires_at", "depth",
                "partial_response", "restart_count", "claimed_by", "owner_agent")},
        })

    return web.json_response({"agent": agent, "messages": messages})


async def handle_agent_queue_delete(request):
    """DELETE /agents/{name}/queue/{queue_id} - cancel one queued message.

    /flush is the all-or-nothing form of this and does not cover it: the
    dashboard cancels a single mistyped message and leaves the rest of the
    backlog alone.

    Marks the row STATUS_SKIPPED rather than deleting it, exactly as
    flush_agent_queue does, so the record of what was asked survives. Only a
    STATUS_QUEUED row can be cancelled — once a message is STATUS_IN_PROGRESS
    it is already inside the subprocess and dropping the row would lose the
    record without stopping the work. Interrupt is the tool for that.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    targets = _resolve_request_targets(request)
    if not targets:
        return web.json_response({"error": "Unknown agent"}, status=404)

    try:
        queue_id = int(request.match_info.get("queue_id", ""))
    except ValueError:
        return web.json_response({"error": "Invalid message id"}, status=400)

    cursor = await db.execute(
        "UPDATE message_queue SET processed = ?"
        f" WHERE id = ? AND agent IN ({','.join('?' * len(targets))}) AND processed = ?",
        (STATUS_SKIPPED, queue_id, *targets, STATUS_QUEUED),
    )
    await db.commit()

    if not cursor.rowcount:
        # No such row, another agent's row, or already picked up. All three
        # mean nothing was cancelled and the caller's list is stale; saying
        # "cancelled" here would be the lie this whole issue is about.
        return web.json_response(
            {"error": "No queued message with that id", "cancelled": False},
            status=404,
        )

    log.info(f"Cancelled queued message {queue_id} for {agent}")
    return web.json_response({"status": "cancelled", "cancelled": True, "id": queue_id})


# Agent name validator — same surface as bin/create-agent.sh's check, used
# to reject path traversal / shell metachars before we touch disk.
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


async def handle_agent_register(request):
    """POST /agents/{name}/register - Hot-load a newly-created agent.

    bin/create-agent.sh writes the new agent into config/agents.yaml and
    then POSTs here so the running server picks it up without a full
    restart. This endpoint:
      1. re-reads agents.yaml (and channels.json) via load_config()
      2. confirms the new agent now appears in agent_config
      3. starts its subprocess (the same code path startup() uses)

    Returns 200 once the subprocess is launched, 404 if the new agent
    didn't show up in the reloaded config (typo / wrong file), and 409
    if the agent is already running.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if not agent or not _AGENT_NAME_RE.match(agent):
        return web.json_response({"error": "Invalid agent name"}, status=400)

    old_specs = list(effective_specs())
    if agent in agent_processes or any(
            sp.id in agent_processes for sp in old_specs if sp.agent == agent):
        return web.json_response(
            {"error": "Agent already running", "agent": agent},
            status=409,
        )

    # Re-read agents.yaml + channels.json so the new entry, its Discord
    # token mapping, and any new channel routing all become visible to
    # the running server.
    await load_config()

    if agent not in agent_config or not shards_lib.resolve_targets(
            effective_specs(), agent):
        return web.json_response(
            {
                "error": (
                    f"Agent '{agent}' not found in config after reload — "
                    "verify it was written to config/agents.yaml"
                )
            },
            status=404,
        )

    log.info(f"Hot-registering new agent: {agent}")
    changes = await sync_shards(effective_specs(), old_specs=old_specs)
    # A shard that exists but has no process (its spawn failed earlier) is
    # started here too, as the single-process path always did.
    for t in shards_lib.resolve_targets(effective_specs(), agent):
        if t not in agent_processes and t not in changes["added"]:
            if t not in agent_locks:
                agent_locks[t] = asyncio.Lock()
                response_buffers[t] = ""
            agent_states.setdefault(t, "IDLE")
            await start_agent_subprocess(t)

    discord_bound = agent in AGENT_TOKENS
    return web.json_response(
        {
            "status": "registered",
            "agent": agent,
            "discord_bound": discord_bound,
        }
    )


async def handle_cost(request):
    """POST /cost - Record external cost event"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()
    cost_delta = data.get("cost_delta", 0.0)

    # A shard id, or an agent id (its first shard).
    agent = shards_lib.first_shard(effective_specs(), data.get("agent") or "")
    if not agent:
        return web.json_response({"error": "Unknown agent"}, status=400)

    # Record cost
    await db.execute(
        "INSERT INTO cost_events (agent, cost_delta, session_total) VALUES (?, ?, ?)",
        (agent, cost_delta, cost_delta)
    )
    await db.commit()

    # Reset last cost (external sessions are independent)
    agent_last_cost[agent] = 0.0

    return web.json_response({"status": "recorded"})

async def handle_usage(request):
    """GET /usage - rate-limit headroom for every agent.

    The counterpart to /cost. `/cost` answers "what has this spent"; this
    answers "how close is it to being cut off", which is the number that
    actually stops a turn mid-sentence.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    report = await usage_gate.usage_report(STATE)
    rows = report.pop("_rows")
    now = time.time()
    # A limit is an account fact: every agent shows the account's worst window
    # (a rejected type first, else the nearest reset).
    def _rank(r):
        reset = r["resets_at"]
        future = isinstance(reset, (int, float)) and reset > now
        return (r["status"] != "rejected", not future, reset if future else 0)
    row = min(rows, key=_rank) if rows else None

    agents = {}
    for name in agent_config:
        info = {
            "resetsAt": row["resets_at"],
            "rateLimitType": row["rate_limit_type"],
        } if row else None
        progress = rate_limit_window_progress(info) if info else None
        agents[name] = {
            "status": row["status"] if row else None,
            "rate_limit_type": row["rate_limit_type"] if row else None,
            "resets_at": row["resets_at"] if row else None,
            "is_using_overage": bool(row["is_using_overage"]) if row else False,
            "overage_status": row["overage_status"] if row else None,
            # None, never 0 — "no reading yet" and "0% consumed" are opposite
            # answers and must not render as the same number.
            "percent_of_window_used": round(progress * 100, 1) if progress is not None else None,
            "summary": format_usage_report(row),
            "updated_at": row["updated_at"] if row else None,
        }
    for t, w in report["windows"].items():
        r = next(x for x in rows if x["rate_limit_type"] == t)
        p = rate_limit_window_progress(
            {"resetsAt": r["resets_at"], "rateLimitType": t})
        w["percent_of_window_used"] = round(p * 100, 1) if p is not None else None

    return web.json_response({"agents": agents, **report})


async def handle_cost_get_all(request):
    """GET /cost - Get cost summary for every agent.

    dashboard/app/api/cost/route.ts (and the Costs page behind it) has
    called this shape since the dashboard's first commit, but nothing ever
    registered a GET handler for the bare /cost path -- only POST /cost
    (ingest, handle_cost) and GET /cost/{agent} (single-agent summary,
    handle_cost_get below) existed. Every no-agent request 405'd, which
    usePoll() treats as a failed fetch, so the whole Costs page rendered
    "Unable to fetch cost data" on every install.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    daily: Dict[str, float] = {}
    async with db.execute(
        """
        SELECT agent, SUM(cost_delta) as total FROM cost_events
        WHERE timestamp > datetime('now', '-1 day') GROUP BY agent
        """
    ) as cursor:
        async for row in cursor:
            daily[row["agent"]] = row["total"] or 0.0

    monthly: Dict[str, float] = {}
    async with db.execute(
        """
        SELECT agent, SUM(cost_delta) as total FROM cost_events
        WHERE timestamp > datetime('now', '-30 days') GROUP BY agent
        """
    ) as cursor:
        async for row in cursor:
            monthly[row["agent"]] = row["total"] or 0.0

    # Roll shard rows up to their agent through the registry. A row whose shard
    # is no longer in the registry stays under its own key.
    def _by_agent(per_shard):
        out: Dict[str, float] = {}
        for key, total in per_shard.items():
            owner = shard_owner.get(key, key)
            out[owner] = out.get(owner, 0.0) + total
        return out

    return web.json_response({
        "daily": daily,
        "monthly": monthly,
        "by_agent": {"daily": _by_agent(daily), "monthly": _by_agent(monthly)},
        "limits": {
            "daily_limit": COST_DAILY_LIMIT,
            "monthly_limit": COST_MONTHLY_LIMIT,
        },
    })


async def handle_cost_get(request):
    """GET /cost/{agent} - Get cost summary"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("agent")
    specs = effective_specs()
    is_agent_id = any(sp.agent == agent for sp in specs)
    # An agent id sums its shards; anything else (a shard id, or a name the
    # registry does not know, which reads as zeros as it always has) is itself.
    keys = [sp.id for sp in specs if sp.agent == agent] if is_agent_id else [agent]

    async def _total(key, window):
        async with db.execute(
            f"SELECT SUM(cost_delta) as total FROM cost_events "
            f"WHERE agent = ? AND timestamp > datetime('now', '{window}')",
            (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["total"] or 0.0

    per_shard = {k: {"daily": await _total(k, "-1 day"),
                     "monthly": await _total(k, "-30 days"),
                     "session": agent_last_cost.get(k, 0.0)} for k in keys}
    body = {
        "agent": agent,
        "daily": sum(v["daily"] for v in per_shard.values()),
        "monthly": sum(v["monthly"] for v in per_shard.values()),
        "session": sum(v["session"] for v in per_shard.values()),
    }
    if is_agent_id:
        body["shards"] = per_shard
    return web.json_response(body)


async def handle_cost_conversations(request):
    """GET /cost/conversations[?agent=X] - Roll cost_events up by conversation.

    A conversation is one context window: (agent, session_id). Rows written
    before the session_id column existed group under session_id NULL, shown
    to callers as "unknown" rather than silently dropped.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent_filter = request.rel_url.query.get("agent")
    where = "WHERE agent = ?" if agent_filter else ""
    params = (agent_filter,) if agent_filter else ()

    async with db.execute(
        f"""
        SELECT agent, session_id,
               SUM(cost_delta) as cost,
               SUM(input_tokens) as input_tokens,
               SUM(output_tokens) as output_tokens,
               SUM(duration_ms) as duration_ms,
               COUNT(*) as turns,
               MIN(timestamp) as started_at,
               MAX(timestamp) as ended_at
        FROM cost_events
        {where}
        GROUP BY agent, session_id
        ORDER BY ended_at DESC
        LIMIT 200
        """,
        params,
    ) as cursor:
        rows = await cursor.fetchall()

    conversations = []
    for row in rows:
        session_id = row["session_id"]
        conversations.append({
            "agent": row["agent"],
            # Truncated for display, same convention as /health's
            # session_id field — the full id is a bearer-token-adjacent
            # secret (--resume takes it) and callers never need the whole
            # thing to tell conversations apart in a list.
            "session_id": (session_id[:8] if session_id else "unknown"),
            "cost": row["cost"] or 0.0,
            "input_tokens": row["input_tokens"] or 0,
            "output_tokens": row["output_tokens"] or 0,
            "duration_ms": row["duration_ms"] or 0,
            "turns": row["turns"] or 0,
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            # The session currently live for this agent -- lets the
            # dashboard badge the in-progress conversation without ever
            # seeing the real session_id itself.
            "current": bool(session_id) and session_id == agent_sessions.get(row["agent"]),
        })

    return web.json_response({"conversations": conversations})

# =============================================================================
# Hive: buzz and hive call (step 2.3). The rules live in lib/hive.py.
# =============================================================================

_hive_last_reap = 0.0
_HIVE_STATUS = {"unknown_caller": 404, "unknown_target": 404, "depth_exceeded": 409,
                "self_call": 409, "deadlock": 409, "no_available_shard": 409,
                "callee_paused": 409, "caller_not_in_turn": 409, "buzz_limit": 429,
                "queue_full": 503}
_HIVE_DETAIL = {
    "self_call": "A shard cannot call itself; answer directly, or use buzz (which "
                 "does not block) to leave yourself a message.",
    "deadlock": "The target is already waiting, directly or through other calls, "
                "on you; the call would never start. Decide without it or use buzz.",
    "depth_exceeded": f"Hive calls and buzzes nest at most {hive_lib.HIVE_MAX_DEPTH} deep.",
    "buzz_limit": f"At most {hive_lib.HIVE_MAX_BUZZ_PER_TURN} buzzes per turn.",
    "caller_not_in_turn": "A hive call needs a running turn to suspend.",
}


def _hive_err(code, detail=None, **extra):
    body = {"error": code, **extra}
    detail = detail or _HIVE_DETAIL.get(code)
    if detail:
        body["detail"] = detail
    return web.json_response(body, status=_HIVE_STATUS.get(code, 400))


async def _hive_body(request):
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def _hive_insert(row: dict, ignore: bool = False) -> bool:
    cols = list(row)
    cur = await db.execute(
        f"INSERT {'OR IGNORE ' if ignore else ''}INTO message_queue "
        f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(row[c] for c in cols))
    n = cur.rowcount
    await cur.close()
    await db.commit()
    return n > 0


async def _hive_maybe_reap():
    """Skip stale unconsumed reply rows; at most once a minute."""
    global _hive_last_reap
    now = time.time()
    if now - _hive_last_reap < 60:
        return
    _hive_last_reap = now
    await msgqueue.reap_hive_rows(db, now, hive_lib.HIVE_REPLY_TTL_S)


async def hive_cancel_call(call_id: str):
    """The caller gave up (or its turn ended): forget the call and skip its row
    if the callee has not started it. A started row finishes; its reply is late."""
    STATE.hive.open.pop(call_id, None)
    cur = await db.execute(
        "UPDATE message_queue SET processed = ?, response = 'cancelled',"
        " processed_at = CURRENT_TIMESTAMP"
        " WHERE call_id = ? AND reply_to_agent IS NOT NULL AND processed = ?",
        (STATUS_SKIPPED, call_id, STATUS_QUEUED))
    await cur.close()
    await db.commit()


async def hive_cancel_caller(shard: str):
    """Close every open call whose caller is `shard` (its turn is over)."""
    try:
        for cid in STATE.hive.calls_of(shard):
            await hive_cancel_call(cid)
    except Exception as e:
        log.warning(f"hive cancel for {shard} failed: {type(e).__name__}: {e}")


def hive_on_turn_start(shard, batch):
    STATE.hive.buzzes_this_turn[shard] = 0


async def hive_on_turn_end(shard, result):
    rows = result.batch.rows
    first = rows[0] if rows else None
    if first is not None and hive_lib.is_call_row(first):
        result.suppress_post = True
        cid = first["call_id"]
        oc = STATE.hive.open.get(cid)
        dur = int((time.time() - oc.started) * 1000) if oc else None
        md = result.metadata or {}
        if md.get("is_error"):
            body = hive_lib.error_body(cid, "callee_error",
                                       (result.response_text or "")[:500])
        elif not md:
            body = hive_lib.error_body(cid, "callee_failed",
                                       "the callee's turn ended without a result")
        else:
            body = hive_lib.answer_body(cid, result.response_text, dur)
        row = hive_lib.reply_row(first, body)
        if oc is None:
            row["processed"] = STATUS_SKIPPED
            row["response"] = "late"
        await _hive_insert(row, ignore=True)
        msgqueue.notify(first["reply_to_agent"])
    await hive_cancel_caller(shard)


def _hive_known(name):
    """A shard id (an agent id selects the agent's first shard) or None."""
    return shards_lib.first_shard(effective_specs(), name) if isinstance(name, str) else None


async def _hive_queue_counts() -> Dict[str, int]:
    async with db.execute(
        "SELECT agent, COUNT(*) AS n FROM message_queue WHERE processed = ?"
        " GROUP BY agent", (STATUS_QUEUED,)) as cur:
        return {r["agent"]: r["n"] for r in await cur.fetchall()}


def _hive_pick(to, caller, kind, counts):
    specs = effective_specs()
    states = {s.id: agent_states.get(s.id, "IDLE") for s in specs}
    # Spec 2.7: a breaker- or budget-paused shard is unavailable to a call (a
    # governor deferral only holds machine rows, so it does not count). A buzz
    # to a paused shard is accepted and waits.
    paused = set()
    if kind == "call":
        for sp in specs:
            view = operator_pause.paused_view(STATE, sp.id)
            if view and view["reason"] in ("manual", "breaker", "budget"):
                paused.add(sp.id)
    return hive_lib.pick_callee(specs, to, caller, states, counts,
                                STATE.hive.waits_for(), kind=kind, paused=paused)


def _hive_unknown_detail():
    return "known targets: " + ", ".join(sorted(
        {s.agent for s in effective_specs()} | {s.id for s in effective_specs()}))


async def handle_react(request):
    """POST /react {agent, emoji, channel_id?, message_id?}: the `react` action
    of the discord tool. Channel and message default to the agent's active turn."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        return web.json_response({"error": "Invalid JSON"}, status=400)
    shard = _hive_known(data.get("agent"))
    if not shard:
        return web.json_response({"error": "unknown agent"}, status=400)
    emoji = discord_ux.emoji_path(data.get("emoji"))
    if emoji is None:
        return web.json_response(
            {"error": "emoji must be a unicode emoji or name:id for a custom one"}, status=400)
    turn = STATE.active_turns.get(shard)
    channel_id = str(data.get("channel_id") or (turn.channel_id if turn else "") or "")
    message_id = str(data.get("message_id") or "")
    if not message_id and turn:
        message_id = next((m for m in reversed(turn.message_ids) if str(m).isdigit()), "")
    if not channel_id.isdigit() or not message_id.isdigit():
        return web.json_response({"error": "no channel or message to react to"}, status=400)
    if not ux_for_channel_id(channel_id).reactions:
        return web.json_response({"error": f"reactions disabled for {channel_id}"}, status=403)
    if not await add_reaction(shard, channel_id, message_id, emoji):
        return web.json_response({"error": "Discord refused the reaction"}, status=502)
    return web.json_response({"reacted": True, "channel_id": channel_id, "message_id": message_id})


async def handle_hive_buzz(request):
    """POST /hive/buzz {from, to, message}: leave another shard a message."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await _hive_body(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    await _hive_maybe_reap()
    caller = _hive_known(data.get("from"))
    if not caller:
        return _hive_err("unknown_caller")
    message = data.get("message")
    if (not isinstance(message, str) or not message.strip()
            or len(message) > hive_lib.HIVE_MAX_QUESTION_CHARS):
        return web.json_response(
            {"error": "invalid_message",
             "detail": f"message must be 1 to {hive_lib.HIVE_MAX_QUESTION_CHARS} characters"},
            status=400)
    turn = STATE.active_turns.get(caller)
    depth = hive_lib.next_depth(turn.rows) if turn else 1
    if depth > hive_lib.HIVE_MAX_DEPTH:
        return _hive_err("depth_exceeded")
    hs = STATE.hive
    if hs.buzzes_this_turn.get(caller, 0) >= hive_lib.HIVE_MAX_BUZZ_PER_TURN:
        return _hive_err("buzz_limit")
    counts = await _hive_queue_counts()
    callee, err = _hive_pick(data.get("to"), caller, "buzz", counts)
    if err:
        return _hive_err(err, _hive_unknown_detail() if err == "unknown_target" else None)
    if counts.get(callee, 0) >= QUEUE_DEPTH_LIMIT:
        return _hive_err("queue_full")
    row = hive_lib.buzz_row(label_of(caller), callee, spec_of(callee).agent, message, depth)
    await _hive_insert(row)
    hs.buzzes_this_turn[caller] = hs.buzzes_this_turn.get(caller, 0) + 1
    msgqueue.notify(callee)
    turn_loop.notify_enqueued(STATE, callee, "0")
    return web.json_response({"status": "queued", "to": callee,
                              "message_id": row["message_id"]}, status=202)


async def handle_hive_call_create(request):
    """POST /hive/call {from, to, question, timeout?}: a blocking question."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await _hive_body(request)
    if data is None:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    await _hive_maybe_reap()
    hs = STATE.hive
    hs.prune()
    caller = _hive_known(data.get("from"))
    if not caller:
        return _hive_err("unknown_caller")
    if agent_states.get(caller) != "PROCESSING":
        return _hive_err("caller_not_in_turn")
    question = data.get("question")
    if (not isinstance(question, str) or not question.strip()
            or len(question) > hive_lib.HIVE_MAX_QUESTION_CHARS):
        return web.json_response(
            {"error": "invalid_question",
             "detail": f"question must be 1 to {hive_lib.HIVE_MAX_QUESTION_CHARS} characters"},
            status=400)
    timeout = hive_lib.clamp_timeout(data.get("timeout", hive_lib.HIVE_DEFAULT_TIMEOUT_S))
    turn = STATE.active_turns.get(caller)
    depth = hive_lib.next_depth(turn.rows) if turn else 1
    if depth > hive_lib.HIVE_MAX_DEPTH:
        return _hive_err("depth_exceeded")
    counts = await _hive_queue_counts()
    callee, err = _hive_pick(data.get("to"), caller, "call", counts)
    if err:
        return _hive_err(err, _hive_unknown_detail() if err == "unknown_target" else None)
    if counts.get(callee, 0) >= QUEUE_DEPTH_LIMIT:
        return _hive_err("queue_full")
    call_id = hive_lib.new_call_id()
    now = time.time()
    row = hive_lib.call_row(call_id, caller, label_of(caller), callee,
                            spec_of(callee).agent, question, depth, timeout, now)
    hs.open[call_id] = hive_lib.OpenCall(caller, callee, depth, now + timeout, now)
    try:
        await _hive_insert(row)
    except Exception:
        hs.open.pop(call_id, None)
        raise
    msgqueue.notify(callee)
    turn_loop.notify_enqueued(STATE, callee, "0")
    return web.json_response(
        {"call_id": call_id, "to": callee, "depth": depth,
         "deadline": msgqueue.utc_iso(now + timeout)}, status=202)


async def _hive_fetch(sql, params=()):
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


def _hive_reply_view(call, reply):
    """The GET /hive/call/{id} body for a reply row."""
    body = hive_lib.parse_body(reply["content"])
    cid = call["call_id"]
    err = body.get("error")
    if not err:
        return {"status": "answered", "answer": body.get("answer", ""),
                "from": call["agent"], "call_id": cid,
                "duration_ms": body.get("duration_ms"),
                **({"truncated": True} if body.get("truncated") else {})}
    out = {"status": "expired" if err == "expired" else "error", "error": err,
           "call_id": cid}
    if body.get("detail"):
        out["detail"] = body["detail"]
    return out


async def handle_hive_call_get(request):
    """GET /hive/call/{call_id}?wait=<s>: long poll for the caller."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    cid = request.match_info["call_id"]
    try:
        wait = max(0.0, float(request.query.get("wait", "0")))
    except ValueError:
        wait = 0.0
    wait = min(wait, hive_lib.HIVE_POLL_WAIT_S)
    hs = STATE.hive
    hs.prune()
    calls = await _hive_fetch(
        "SELECT * FROM message_queue WHERE call_id = ? AND reply_to_agent IS NOT NULL"
        " ORDER BY id LIMIT 1", (cid,))
    if not calls:
        return web.json_response({"error": "unknown_call"}, status=404)
    call = calls[0]
    caller, callee = call["reply_to_agent"], call["agent"]
    oc = hs.open.get(cid)
    exp = hive_lib._epoch(call["expires_at"])
    deadline = (oc.deadline if oc else (exp or time.time())) + hive_lib.HIVE_DEADLINE_SLACK_S
    wait_end = time.time() + wait
    while True:
        # Read before any check: a reply notified after this point must not be
        # missed by the wait below (the checks are awaits; the notify can land
        # between them and the wait).
        seen = msgqueue.work_version(caller)
        # Produce 1.2's expiry reply even while the callee is busy in a turn
        # (claim_batch is the only other place expire runs).
        await msgqueue.expire(db, callee)
        replies = await _hive_fetch(
            "SELECT * FROM message_queue WHERE call_id = ? AND reply_to_agent IS NULL"
            " AND processed IN (?, ?) ORDER BY id LIMIT 1",
            (cid, STATUS_QUEUED, STATUS_COMPLETE))
        if replies:
            reply = replies[0]
            if reply["processed"] == STATUS_QUEUED:
                await db.execute(
                    "UPDATE message_queue SET processed = ?,"
                    " processed_at = CURRENT_TIMESTAMP WHERE id = ? AND processed = ?",
                    (STATUS_COMPLETE, reply["id"], STATUS_QUEUED))
                await db.commit()
            hs.open.pop(cid, None)
            return web.json_response(_hive_reply_view(call, reply))
        fresh = (await _hive_fetch(
            "SELECT processed, response FROM message_queue WHERE id = ?",
            (call["id"],)))[0]
        proc, resp = fresh["processed"], fresh["response"]
        if proc == STATUS_CRASHED:
            hs.open.pop(cid, None)
            return web.json_response({"status": "error", "error": "callee_failed",
                                      "call_id": cid})
        if proc == STATUS_SKIPPED and resp != "expired":
            hs.open.pop(cid, None)
            return web.json_response({"status": "error", "error": "cancelled",
                                      "call_id": cid})
        if proc in (STATUS_QUEUED, STATUS_IN_PROGRESS) and cid not in hs.open:
            return web.json_response({"status": "error", "error": "cancelled",
                                      "call_id": cid})
        now = time.time()
        if now >= deadline:
            await hive_cancel_call(cid)
            return web.json_response({"status": "timeout", "call_id": cid})
        if now >= wait_end:
            return web.json_response({"status": "pending", "call_id": cid})
        slice_s = min(wait_end - now, deadline - now,
                      0.25 if proc == STATUS_QUEUED else hive_lib.HIVE_POLL_WAIT_S)
        await msgqueue.wait_for_work(caller, max(0.05, slice_s), since=seen)


async def handle_hive_call_cancel(request):
    """POST /hive/call/{call_id}/cancel."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    await hive_cancel_call(request.match_info["call_id"])
    return web.json_response({"status": "cancelled"})


async def _graph_browse(request, route, entity_id=None):
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    from lib.graph import browse
    loop = asyncio.get_running_loop()
    status, body = await loop.run_in_executor(
        None, browse.handle, route, dict(request.query), WORKSPACE_ROOT / "data", entity_id)
    return web.json_response(body, status=status)


async def handle_graph_status(request):
    """GET /graph/status (docs/graph-browse-api.md)."""
    return await _graph_browse(request, "status")


async def handle_graph_observations(request):
    """GET /graph/observations."""
    return await _graph_browse(request, "observations")


async def handle_graph_entities(request):
    """GET /graph/entities."""
    return await _graph_browse(request, "entities")


async def handle_graph_entity(request):
    """GET /graph/entities/{id}."""
    return await _graph_browse(request, "entity", request.match_info["id"])


async def handle_hive_calls(request):
    """GET /hive/calls: the call log (docs/hive-call-log.md)."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    await _hive_maybe_reap()
    STATE.hive.prune()
    q = request.query
    try:
        limit = max(1, min(500, int(q.get("limit", "100"))))
    except ValueError:
        limit = 100
    where, params = ["call_id IS NOT NULL", "reply_to_agent IS NOT NULL"], []
    if q.get("shard"):
        where.append("(agent = ? OR reply_to_agent = ?)")
        params += [q["shard"], q["shard"]]
    if q.get("since"):
        where.append("created_at >= ?")
        params.append(q["since"].replace("T", " ").rstrip("Z"))
    status_filter = q.get("status")
    sql = ("SELECT * FROM message_queue WHERE " + " AND ".join(where)
           + " ORDER BY created_at DESC, id DESC")
    if not status_filter:
        sql += " LIMIT ?"
        params.append(limit)
    calls = await _hive_fetch(sql, tuple(params))
    replies = {}
    if calls:
        ids = [c["call_id"] for c in calls]
        for r in await _hive_fetch(
                "SELECT * FROM message_queue WHERE reply_to_agent IS NULL AND call_id IN"
                f" ({','.join('?' * len(ids))}) ORDER BY id", tuple(ids)):
            replies.setdefault(r["call_id"], r)
    specs = effective_specs()
    now = time.time()
    out = []
    for c in calls:
        reply = replies.get(c["call_id"])
        status = hive_lib.call_status(c, reply, c["call_id"] in STATE.hive.open, now)
        if status_filter and status != status_filter:
            continue
        out.append(hive_lib.log_entry(c, reply, status, specs))
        if len(out) >= limit:
            break
    return web.json_response({"calls": out})


async def hive_startup_sweep():
    """A restart killed every shard subprocess: no caller waits on a queued call
    or reply, so none may run later."""
    cur = await db.execute(
        "UPDATE message_queue SET processed = ?, response = 'abandoned',"
        " processed_at = CURRENT_TIMESTAMP"
        " WHERE call_id IS NOT NULL AND processed = ?",
        (STATUS_SKIPPED, STATUS_QUEUED))
    await cur.close()
    await db.commit()
    await msgqueue.reap_hive_rows(db, time.time(), hive_lib.HIVE_REPLY_TTL_S)


def _beacon_state(shard):
    return agent_states.get(shard) or "PROCESSING"


def beacon_on_turn_start(shard, batch):
    beacon_extra[shard] = {"phase": "awaiting_first_event", "last_event_type": None,
                           "current_tool": None}
    write_agent_beacon(shard, _beacon_state(shard), force=True)


def beacon_on_event(shard, event):
    """Track what the turn is doing. Never raises into the turn."""
    try:
        ex = beacon_extra.setdefault(shard, {})
        etype = event.get("type") if isinstance(event, dict) else None
        prev_phase = ex.get("phase")
        ex["last_event_type"] = etype
        phase = prev_phase or "streaming"
        if etype == "assistant":
            blocks = (event.get("message") or {}).get("content") or []
            tool = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"), None)
            if tool and not event.get("parent_tool_use_id"):
                phase = "tool_running"
                ex["current_tool"] = {"name": tool.get("name"), "started_at": time.time()}
            else:
                phase = "streaming"
        elif etype == "user":
            phase, ex["current_tool"] = "streaming", None
        elif etype == "result":
            phase, ex["current_tool"] = "closing", None
        ex["phase"] = phase
        write_agent_beacon(shard, _beacon_state(shard), force=phase != prev_phase)
    except Exception as e:
        log.debug(f"beacon on_event for {shard} failed: {e}")


def beacon_on_turn_end(shard, result):
    try:
        beacon_extra[shard] = {"phase": "closing", "last_event_type": "result",
                               "current_tool": None}
    except Exception:
        pass


def register_beacon_hooks():
    for name, fn in (("on_turn_start", beacon_on_turn_start),
                     ("on_event", beacon_on_event),
                     ("on_turn_end", beacon_on_turn_end)):
        if fn not in getattr(STATE.hooks, name):
            getattr(STATE.hooks, name).append(fn)


def register_hive_hooks():
    """Hive's on_turn_end runs first, ahead of any later hook (2.6's handoff
    registers after it)."""
    for name, fn in (("on_turn_start", hive_on_turn_start),
                     ("on_turn_end", hive_on_turn_end)):
        if fn not in getattr(STATE.hooks, name):
            getattr(STATE.hooks, name).insert(0, fn)


# =============================================================================
# Session policy: context budget, handoff note, reset (step 2.6)
# =============================================================================
# The logic is in lib/session_policy.py; this is the wiring. Hook order:
# hive's on_turn_end runs first (a reply row is written before any reset), this
# one runs last and only schedules work (followups run after the shard lock is
# released).

def register_session_policy(state=None):
    """Register the policy hooks once, after the hive's. Idempotent."""
    state = state or STATE
    for name, fn in (("on_turn_end", session_policy_on_turn_end),
                     ("on_event", session_policy_on_event),
                     ("before_claim", session_policy_before_claim)):
        if fn not in getattr(state.hooks, name):
            state.hooks.register(name, fn)


def session_policy_before_claim(shard):
    """Truthy while a reset is restarting this shard: no turn may start on the
    old session. begin_reset kicks the drain once the new one is up."""
    return bool(STATE.session_policy.resetting.get(shard))


def session_policy_on_event(shard, event):
    if event.get("type") == "system" and event.get("subtype") == "compact_boundary":
        meta = event.get("compact_metadata") or {}
        STATE.session_policy.compact_event[shard] = {
            "pre": event.get("pre_tokens", meta.get("pre_tokens")),
            "post": event.get("post_tokens", meta.get("post_tokens"))}


def session_policy_on_turn_end(shard, result):
    sp = STATE.session_policy
    rows = result.batch.rows
    if sp_lib.is_internal_batch(rows):
        # A handoff or compact reply is never posted, whatever channel_id says.
        result.suppress_post = True
        if sp.compacting.get(shard):
            return          # begin_reset is awaiting this row itself
        if shard in sp.inflight:
            # Stop new turns starting on this session before the lock is
            # released; finish_reset restarts the shard.
            sp.resetting[shard] = True
            result.followups.append(lambda: finish_reset(shard))
        return

    cfg = STATE.cfg(shard)
    budget = cfg.get("context_budget_tokens")
    requested = sp.reset_requested.get(shard)
    md = result.metadata or {}
    # No budget and no request: only an errored turn can still mean overflow
    # (a regex on text already in hand, no database access).
    if not budget and not requested and not md.get("is_error"):
        return
    overflow = bool(md.get("is_error")) and bool(
        CONTEXT_OVERFLOW_RE.search(result.raw_response_text or ""))
    reason = sp_lib.should_reset(md.get("context_tokens", 0) or 0, budget, overflow)
    if reason is None and requested:
        reason = requested
    if reason is None:
        return
    if any(r["call_id"] for r in rows):
        # The caller is waiting inside this turn; the reset is taken at the
        # next ordinary turn end (a pending agent request stays pending).
        log.warning(f"{shard}: reset ({reason}) deferred past a hive call turn")
        return
    sp.reset_requested.pop(shard, None)
    since = time.time() - sp.last_reset_at.get(shard, 0)
    if since < sp_lib.RESET_MIN_INTERVAL_S or shard in sp.inflight:
        log.warning(f"{shard}: reset ({reason}) skipped: "
                    + ("one is already running" if shard in sp.inflight
                       else f"last reset {int(since)}s ago"))
        return
    result.followups.append(lambda: begin_reset(shard, reason))


async def _insert_internal_row(shard, prefix, content, timeout_s):
    """One server-inserted row (handoff or compact turn): an ordinary queue row
    in channel `handoff`, claimed alone, priority HANDOFF_PRIORITY."""
    message_id = f"{prefix}-{shard}-{uuid.uuid4().hex[:12]}"
    expires = msgqueue.utc_iso(time.time() + timeout_s)
    await db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
        " is_bot, content, message_id, priority, owner_agent, expires_at)"
        " VALUES (?, ?, '0', 'local', 'session-handoff', 1, ?, ?, ?, ?, ?)",
        (shard, sp_lib.HANDOFF_CHANNEL, content, message_id,
         sp_lib.HANDOFF_PRIORITY, STATE.agent_of(shard), expires))
    await db.commit()
    msgqueue.notify(shard)
    turn_loop.notify_enqueued(STATE, shard, "0")
    return message_id


async def _wait_internal_row(shard, message_id, timeout_s) -> bool:
    """Wait for the row to leave QUEUED/IN_PROGRESS. True when it did. On
    timeout the row is skipped (still queued) or its turn interrupted."""
    sp = STATE.session_policy
    deadline = time.monotonic() + timeout_s
    while True:
        async with db.execute("SELECT processed FROM message_queue"
                              " WHERE message_id = ?", (message_id,)) as cur:
            row = await cur.fetchone()
        status = row["processed"] if row else STATUS_SKIPPED
        if status not in (STATUS_QUEUED, STATUS_IN_PROGRESS):
            return True
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    log.warning(f"{shard}: internal turn {message_id} not done after {timeout_s}s")
    if status == STATUS_QUEUED:
        await db.execute("UPDATE message_queue SET processed = ?, response = 'timeout',"
                         " processed_at = CURRENT_TIMESTAMP"
                         " WHERE message_id = ? AND processed = ?",
                         (STATUS_SKIPPED, message_id, STATUS_QUEUED))
        await db.commit()
    else:
        if shard in sp.inflight:        # a do_reset is still to come and clears this
            sp.resetting[shard] = True  # nothing may claim on the old session
        await interrupt_agent(shard)
    return False


async def _settle(shard):
    """Wait (bounded) for 2.5's steered lines to drain; settled at once when
    steering is absent. On timeout carry on: release_pending returns queued
    steered lines to the queue when the process is killed."""
    steer = getattr(STATE, "steer", None)
    if steer is None:
        return
    deadline = time.monotonic() + sp_lib.SETTLE_WAIT_S
    while time.monotonic() < deadline:
        try:
            if not steer[shard].pending():
                return
        except Exception:
            return
        await asyncio.sleep(0.1)


async def begin_reset(shard, reason):
    """Reset one shard: settle, (optionally) compact or take a handoff note,
    then restart it. Best effort throughout: a failed handoff never blocks the
    reset."""
    sp = STATE.session_policy
    if shard in sp.inflight:
        return
    sp.inflight[shard] = reason
    try:
        await _settle(shard)
        cfg = STATE.cfg(shard)
        if (reason == sp_lib.REASON_BUDGET and cfg.get("reset_mode") == "compact"):
            if not sp_lib.COMPACT_VERIFIED:
                if not sp.compact_warned:
                    sp.compact_warned = True
                    log.warning("reset_mode: compact is unverified against the real "
                                "CLI (COMPACT_VERIFIED is False); behaving as reset")
            elif await _run_compact(shard):
                sp.inflight.pop(shard, None)
                sp.last_reset_at[shard] = time.time()
                turn_loop.spawn(STATE, turn_loop.drain_shard(STATE, shard))
                return
        with_handoff = (
            reason != sp_lib.REASON_OVERFLOW
            and bool(cfg.get("handoff_on_reset"))
            and not await agent_hold_until(shard)
            and not usage_gate.account_paused()
            and not shutting_down)
        if with_handoff:
            await _run_handoff(shard)
    except Exception as e:
        log.error(f"{shard}: handoff before reset failed: {type(e).__name__}: {e}")
    await do_reset(shard)


async def _run_handoff(shard):
    path = sp_lib.handoff_path(WORKSPACE_ROOT, shard)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout_s = sp_lib.HANDOFF_TURN_TIMEOUT_S
    mid = await _insert_internal_row(
        shard, "handoff", sp_lib.build_handoff_prompt(path), timeout_s)
    await _wait_internal_row(shard, mid, timeout_s)


async def _run_compact(shard) -> bool:
    """Compact mode (unverified, off). True when the CLI reported a
    compact_boundary and the turn completed; any failure means reset."""
    sp = STATE.session_policy
    timeout_s = sp_lib.COMPACT_TURN_TIMEOUT_S
    before = (await get_context_tokens()).get(shard, 0)
    sp.compact_event.pop(shard, None)
    sp.compacting[shard] = True
    try:
        mid = await _insert_internal_row(shard, "compact", sp_lib.COMPACT_PROMPT, timeout_s)
        done = await _wait_internal_row(shard, mid, timeout_s)
    finally:
        sp.compacting.pop(shard, None)
    event = sp.compact_event.pop(shard, None)
    async with db.execute("SELECT processed FROM message_queue WHERE message_id = ?",
                          (mid,)) as cur:
        row = await cur.fetchone()
    if not (done and event and row and row["processed"] == STATUS_COMPLETE):
        log.warning(f"{shard}: compact failed (done={done}, boundary={bool(event)}); resetting")
        sp.resetting.pop(shard, None)
        return False
    after = (await get_context_tokens()).get(shard, 0)
    log.info(f"{shard}: compacted tokens_before={event.get('pre') or before} "
             f"tokens_after={event.get('post') or after}")
    return True


async def finish_reset(shard):
    """Scheduled when the handoff turn ends: reset now, not at the timeout."""
    await do_reset(shard)


async def do_reset(shard):
    """Restart the shard on a fresh session (kill, clear, spawn). Claimed once:
    whichever of finish_reset and begin_reset gets here first does it."""
    sp = STATE.session_policy
    if sp.inflight.pop(shard, None) is None:
        return
    sp.last_reset_at[shard] = time.time()
    sp.resetting[shard] = True
    try:
        lock = agent_locks.get(shard)
        if lock is not None:
            async with lock:
                await restart_agent(shard)
        else:
            await restart_agent(shard)
    except Exception as e:
        log.error(f"{shard}: reset failed: {type(e).__name__}: {e}")
    finally:
        sp.resetting.pop(shard, None)
    # Queued human rows run on the fresh session.
    turn_loop.spawn(STATE, turn_loop.drain_shard(STATE, shard))


# =============================================================================
# Graceful Shutdown
# =============================================================================

def _bearer_ok(request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    return auth_header.startswith("Bearer ") and auth_header[7:] == AGENT_SERVER_TOKEN


async def handle_ask_create(request):
    """POST /ask - Put a multiple-choice question to the user in Discord.

    Called by the MCP `ask_user` tool (mcp/tools-server.py), which then polls
    GET /ask/{id} for the answer. The question is posted into the channel the
    agent's current turn came from, as an embed with one button per option.
    """
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # `agent` is a shard id here (the tools server sends KARAKOS_SHARD); an
    # agent id resolves to its first shard.
    agent = shards_lib.first_shard(effective_specs(), data.get("agent") or "")
    if not agent:
        return web.json_response({"error": "Invalid agent"}, status=400)

    context = agent_turn_context.get(agent) or {}
    channel_id = str(data.get("channel_id") or context.get("channel_id") or "0")
    if channel_id == "0":
        return web.json_response(
            {"error": "No Discord channel for this turn; the question has nowhere to go"},
            status=409,
        )

    # Whoever is in this conversation may answer, and so may the owner. An
    # empty set means the turn had no human author (a heartbeat, a poke), in
    # which case anyone in the channel can answer — restricting an unattended
    # question to nobody would just hang it.
    allowed = {str(a) for a in (context.get("author_ids") or ()) if a and str(a) != "0"}
    if allowed and OWNER_DISCORD_ID and OWNER_DISCORD_ID != "0":
        allowed.add(str(OWNER_DISCORD_ID))

    try:
        ask = ask_registry.create(
            agent=agent,
            channel_id=channel_id,
            question=data.get("question", ""),
            options=data.get("options"),
            header=data.get("header"),
            timeout=data.get("timeout"),
            allowed_user_ids=allowed,
        )
    except ask_handler.AskError as e:
        return web.json_response({"error": str(e)}, status=400)

    # Posted under the relay's own token so the click has somewhere to land.
    poster = gateway_agent() or agent
    message_id = await post_discord_payload(poster, channel_id, ask_registry.payload_for(ask))
    if not message_id:
        ask_registry.discard_agent(agent)
        return web.json_response(
            {"error": "Could not post the question to Discord"}, status=502
        )
    ask.message_id = message_id

    # A person deciding takes minutes, during which the subprocess emits
    # nothing. Park the beacon somewhere wedge-check.py does not treat as an
    # active turn, or every ask pages as a hang.
    write_agent_beacon(agent, ask_handler.AWAITING_USER_STATE, force=True)
    log.info(f"{agent} asked a question in {channel_id} (ask {ask.ask_id}, msg {message_id})")

    return web.json_response(ask.status(), status=201)


async def handle_ask_status(request):
    """GET /ask/{ask_id} - Poll for the answer."""
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    for expired in ask_registry.sweep():
        # Nobody clicked. Hand the agent's beacon back to the wedge detector
        # so a genuinely hung turn after a timed-out question is still seen.
        write_agent_beacon(expired.agent, "PROCESSING", force=True)
        log.info(f"{expired.agent} question {expired.ask_id} expired unanswered")

    ask = ask_registry.get(request.match_info.get("ask_id", ""))
    if ask is None:
        return web.json_response({"status": "unknown"}, status=404)
    return web.json_response(ask.status())


async def handle_ask_answer(request):
    """POST /ask/{ask_id}/answer - Record a button click.

    Always 200 with an `outcome` field, including for an unknown ask. The
    caller is bin/relay.py turning this into a line of text for the person
    who clicked, and every outcome — expired, already answered, not your
    question — is something they need told. A bare 404 would give them
    nothing but a dead button, which is the failure this whole feature
    exists to remove.
    """
    if not _bearer_ok(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    ask_id = request.match_info.get("ask_id", "")
    index = data.get("index")
    outcome, ask = ask_registry.answer(
        ask_id, index, data.get("user_id"), data.get("user_name")
    )

    if outcome == "answered" and ask is not None:
        # The turn is moving again; restore the beacon the ask parked.
        write_agent_beacon(ask.agent, "PROCESSING", force=True)
        log.info(
            f"{ask.agent} question {ask.ask_id} answered "
            f"'{ask.answer_label}' by {ask.answered_by}"
        )

    return web.json_response({
        "outcome": outcome,
        "note": ask_handler.resolution_note(outcome, ask),
        "answer": ask.answer_label if ask is not None else None,
    })


async def graceful_shutdown(sig):
    """Handle SIGTERM gracefully"""
    global shutting_down
    log.info(f"Received {sig}, shutting down gracefully...")
    shutting_down = True

    # Stop accepting new messages (set flag checked by handlers)

    # Wait for every shard to finish (max 30s)
    log.info("Waiting for agents to finish current messages...")
    for i in range(30):
        all_idle = all(agent_states.get(sid) == "IDLE" for sid in STATE.shard_ids())
        if all_idle:
            break
        await asyncio.sleep(1)

    # No summarizer and no handoff turn here: the stop timeout cannot hold a
    # model turn. Sessions persist and the next boot resumes them.
    await turn_loop.cancel_background(STATE)
    for t in list(getattr(getattr(STATE, "operator_pause", None), "timers", {}).values()):
        t.cancel()
    # Kill subprocesses
    log.info("Terminating agent subprocesses...")
    for agent in list(agent_processes.keys()):
        await kill_agent_subprocess(agent)

    # Close DB
    if db:
        await db.close()

    if _outbox_task is not None:
        _outbox_task.cancel()
        try:
            await _outbox_task
        except (asyncio.CancelledError, Exception):
            pass

    # Close HTTP session
    if http_session:
        await http_session.close()

    log.info("Shutdown complete")


async def _shutdown_and_exit(sig):
    """Signal-handler task. Exits via os._exit, not sys.exit: a SystemExit raised
    inside a task is never retrieved, so asyncio logged it as an unhandled task error."""
    await graceful_shutdown(sig)
    logging.shutdown()
    os._exit(0)

# =============================================================================
# Server Startup
# =============================================================================

async def drain_all_shards_at_startup():
    """Drain every shard's queued rows once at boot. Rows queued before a
    restart have no enqueue event to wake them. drain_shard returns when
    nothing is claimable, and claim honours not_before, so held rows stay held."""
    for sid in STATE.shard_ids():
        turn_loop.spawn(STATE, turn_loop.drain_shard(STATE, sid))


async def startup(app):
    """Initialize server on startup"""
    global http_session

    # Refuse to boot on unstamped/old data before any DB is opened. The
    # migrator is the only writer of 1.x data; boot only checks the stamp.
    require_stamp(WORKSPACE_ROOT / "data")

    log.info("Starting Karakos Agent Server")

    redact.register_literals(os.environ)

    # Hive first, so its on_turn_end precedes every other turn hook.
    register_hive_hooks()
    register_beacon_hooks()

    # Initialize HTTP session
    http_session = aiohttp.ClientSession()

    # Initialize database
    await init_db()

    # Load configuration
    await load_config()

    # Account breaker, token budget and weekly governor (spec 2.7).
    usage_gate.install(STATE)

    # Effort change after a turn (spec 6.4): hold claims, respawn, drain.
    STATE.hooks.register("before_claim", effort_before_claim)
    STATE.hooks.register("on_turn_end", effort_on_turn_end)

    # Operator pause (spec 6.4): /pause holds the queue, /resume releases it.
    operator_pause.install(
        STATE, on_change=lambda sh: write_agent_beacon(
            sh, agent_states.get(sh, "IDLE"), force=True))

    # Session policy last: it only schedules work, after hive and the gate.
    register_session_policy(STATE)

    # Initialize locks and state for every shard before any spawn.
    for sid in STATE.shard_ids():
        agent_locks[sid] = asyncio.Lock()
        agent_states[sid] = "IDLE"
        response_buffers[sid] = ""
        # Overwrite any beacon left behind by a previous process. A crash
        # mid-turn leaves one reading PROCESSING with a timestamp that will
        # never advance again — which is indistinguishable from a live wedge,
        # so without this every restart-after-crash pages forever about an
        # agent that is now fine.
        write_agent_beacon(sid, "IDLE", force=True)
    # Timers for pauses that survived the restart (locks exist now).
    operator_pause.rearm_all(STATE)

    # Rows keyed by an agent's own id that no shard of that agent carries are
    # unreachable history; say so, never fail over it.
    try:
        async with db.execute("SELECT agent FROM sessions") as cur:
            session_keys = [r["agent"] for r in await cur.fetchall()]
        async with db.execute("SELECT DISTINCT agent FROM message_queue") as cur:
            queue_keys = [r["agent"] for r in await cur.fetchall()]
        for line in shards_lib.orphan_key_warnings(effective_specs(), session_keys, queue_keys):
            log.warning(line)
    except Exception as e:
        log.warning(f"orphan-key check skipped: {e}")

    # Outbox delivery loop (recovers rows a crash left in `sending` first).
    global _outbox_task
    _outbox_task = asyncio.create_task(outbox_loop())

    # Crash recovery
    await crash_recovery()
    await hive_startup_sweep()

    # Start shard subprocesses, one at a time in plan order.
    for sid in STATE.shard_ids():
        await start_agent_subprocess(sid)

    # Re-arm replay timers for batches held behind a usage wall before restart.
    for sid in STATE.shard_ids():
        held_until = await agent_hold_until(sid)
        if held_until:
            schedule_hold_wake(sid, held_until)

    await drain_all_shards_at_startup()

    # Register signal handlers in event loop context
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_shutdown_and_exit("SIGTERM")))
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(_shutdown_and_exit("SIGINT")))

    log.info(f"Agent server ready on port {PORT}")

async def shutdown(app):
    """Cleanup on shutdown"""
    log.info("Server shutdown initiated")

    # Background drain/steal/steer tasks first: none may touch the db or a
    # process after they are closed.
    await turn_loop.cancel_background(STATE)

    # Kill all subprocesses
    for agent in list(agent_processes.keys()):
        await kill_agent_subprocess(agent)

    # Close HTTP session
    if http_session:
        await http_session.close()

    # Close database
    if db:
        await db.close()

# =============================================================================
# Main
# =============================================================================

def create_app(with_lifecycle: bool = True) -> web.Application:
    """Build the aiohttp app — the server's whole routing surface.

    Split out of main() so tests can drive the real route table over real
    HTTP. A test that calls a handler directly proves the handler works and
    says nothing about whether the URL reaches it, which is the half that
    breaks. `with_lifecycle=False` skips the startup/shutdown hooks (sqlite,
    subprocess spawning, signal handlers) that a route test supplies itself.
    """
    app = web.Application()

    app.router.add_post("/message", handle_message)
    app.router.add_post("/message/edit", handle_message_edit)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/outbox", handle_outbox_list)
    app.router.add_get("/outbox/{id}", handle_outbox_get)
    app.router.add_post("/outbox/{id}/retry", handle_outbox_retry)
    app.router.add_post("/outbox/{id}/discard", handle_outbox_discard)
    app.router.add_get("/agents", handle_agents)
    app.router.add_post("/agents/{name}/reset", handle_agent_reset)
    app.router.add_post("/agents/{name}/reload", handle_agent_reload)
    app.router.add_post("/agents/{name}/session/finalize", handle_session_finalize)
    app.router.add_post("/agents/{name}/register", handle_agent_register)
    app.router.add_post("/agents/{name}/interrupt", handle_agent_interrupt)
    app.router.add_post("/agents/{name}/effort", handle_agent_effort)
    app.router.add_post("/agents/{name}/model", handle_agent_model)
    app.router.add_post("/agents/{name}/pause", handle_agent_pause)
    app.router.add_post("/agents/{name}/resume", handle_agent_resume)
    app.router.add_post("/agents/{name}/kill", handle_agent_kill)
    app.router.add_post("/agents/{name}/flush", handle_agent_flush)
    app.router.add_get("/agents/{name}/queue", handle_agent_queue)
    app.router.add_delete("/agents/{name}/queue/{queue_id}", handle_agent_queue_delete)
    app.router.add_post("/cost", handle_cost)
    app.router.add_get("/cost", handle_cost_get_all)
    # Registered before the /cost/{agent} pattern below so "conversations"
    # is never captured as an agent name.
    app.router.add_get("/cost/conversations", handle_cost_conversations)
    app.router.add_get("/cost/{agent}", handle_cost_get)
    app.router.add_get("/usage", handle_usage)
    app.router.add_post("/ask", handle_ask_create)
    app.router.add_get("/ask/{ask_id}", handle_ask_status)
    app.router.add_post("/ask/{ask_id}/answer", handle_ask_answer)
    app.router.add_post("/react", handle_react)
    app.router.add_post("/hive/buzz", handle_hive_buzz)
    app.router.add_post("/hive/call", handle_hive_call_create)
    app.router.add_get("/hive/call/{call_id}", handle_hive_call_get)
    app.router.add_post("/hive/call/{call_id}/cancel", handle_hive_call_cancel)
    app.router.add_get("/hive/calls", handle_hive_calls)
    app.router.add_get("/graph/status", handle_graph_status)
    app.router.add_get("/graph/observations", handle_graph_observations)
    app.router.add_get("/graph/entities", handle_graph_entities)
    app.router.add_get("/graph/entities/{id}", handle_graph_entity)

    # Register startup/shutdown handlers
    if with_lifecycle:
        app.on_startup.append(startup)
        app.on_shutdown.append(shutdown)

    return app


def main():
    """Main entry point"""
    require_stamp(WORKSPACE_ROOT / "data")
    # Signal handlers will be registered after event loop starts (in startup)
    # For now, just set flag to handle in asyncio context
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)

if __name__ == "__main__":
    main()
