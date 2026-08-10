#!/usr/bin/env python3
"""
Karakos Agent Server — Persistent Subprocess Architecture

Accepts messages via HTTP, queues to SQLite, sends to persistent claude
subprocess via stdin (stream-json), posts responses to Discord.

Port: 18791 (configurable via AGENT_SERVER_PORT env var)
"""

import asyncio
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
import ask_handler  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
PORT = int(os.environ.get("AGENT_SERVER_PORT", "18791"))
DB_PATH = WORKSPACE_ROOT / "data" / "memory" / "agent-server.db"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.json"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
CLAUDE_SETTINGS_PATH = WORKSPACE_ROOT / "config" / "claude-settings.json"
STREAM_LOG_DIR = WORKSPACE_ROOT / "logs" / "agent-streams"
DEAD_LETTER_PATH = WORKSPACE_ROOT / "data" / "discord-dead-letter.jsonl"
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
OWNER_DISCORD_ID = os.environ.get("OWNER_DISCORD_ID", "0")

# Attempts per chunk before a reply is dead-lettered. Applies to failures that
# might clear on their own (5xx, network); a 403 is not one of those and is
# dead-lettered on the first try — see post_to_discord.
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

# Processing states
STATUS_QUEUED = 0
STATUS_IN_PROGRESS = 1
STATUS_COMPLETE = 2
STATUS_CRASHED = 3
STATUS_SKIPPED = 4

# Session persistence
SUMMARY_DIR = WORKSPACE_ROOT / "logs" / "session-summaries"
LAST_SUMMARY_TEMPLATE = WORKSPACE_ROOT / "data" / "last-session-summary-{agent}.md"

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
channels_config: Dict[str, Any] = {}
agent_processes: Dict[str, asyncio.subprocess.Process] = {}
agent_locks: Dict[str, asyncio.Lock] = {}
agent_states: Dict[str, str] = {}
response_buffers: Dict[str, str] = {}
agent_last_cost: Dict[str, float] = {}
agent_sessions: Dict[str, str] = {}
stderr_reader_tasks: Dict[str, asyncio.Task] = {}
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

# Who and where the agent's current turn came from. `/ask` has no channel of
# its own — the question belongs in the conversation that prompted it — and
# the authors of that conversation are the people allowed to answer it.
agent_turn_context: Dict[str, Dict[str, Any]] = {}

# Discord token mapping
AGENT_TOKENS: Dict[str, str] = {}
DISCORD_ID_TO_AGENT: Dict[int, str] = {}

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
            processed_at TIMESTAMP
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

    # Sessions table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            agent TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            compaction_count INTEGER DEFAULT 0,
            last_compacted TIMESTAMP
        )
    """)

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

    # Rate-limit state table. One row per agent, overwritten — this is a
    # current-headroom reading, not a history, and the CLI resends it.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_state (
            agent TEXT PRIMARY KEY,
            status TEXT,
            rate_limit_type TEXT,
            resets_at INTEGER,
            overage_status TEXT,
            is_using_overage INTEGER DEFAULT 0,
            alerted_for_resets_at INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrations for databases created before a column existed. CREATE TABLE
    # IF NOT EXISTS is a no-op against an existing table, so a new column in
    # the definition above reaches upgraded installs only through here.
    await ensure_column("message_queue", "attachments", "TEXT")

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

    # Load agents config
    if AGENTS_CONFIG_PATH.exists():
        with open(AGENTS_CONFIG_PATH) as f:
            config_data = json.load(f)
            agent_config = config_data.get("agents", {})
            log.info(f"Loaded configuration for {len(agent_config)} agents")
    else:
        log.error(f"Agents config not found: {AGENTS_CONFIG_PATH}")
        agent_config = {}

    # Load channels config
    if CHANNELS_CONFIG_PATH.exists():
        with open(CHANNELS_CONFIG_PATH) as f:
            channels_config = json.load(f)
            log.info(f"Loaded {len(channels_config.get('channels', {}))} channel mappings")
    else:
        log.warning(f"Channels config not found: {CHANNELS_CONFIG_PATH}")
        channels_config = {}

    # Build Discord token map
    for agent_name, config in agent_config.items():
        token_env_var = config.get("discord_bot_token_env")
        if token_env_var:
            token = os.environ.get(token_env_var, "")
            if token:
                AGENT_TOKENS[agent_name] = token
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

async def clear_session(agent: str):
    """Clear agent session and create new ID"""
    session_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO sessions (agent, session_id, input_tokens, compaction_count)
        VALUES (?, ?, 0, 0)
        ON CONFLICT(agent) DO UPDATE SET
            session_id = ?,
            input_tokens = 0,
            compaction_count = 0,
            last_compacted = CURRENT_TIMESTAMP
        """,
        (agent, session_id, session_id)
    )
    await db.commit()
    agent_last_cost.pop(agent, None)
    log.info(f"Cleared session for {agent}, new ID: {session_id}")

async def update_session_tokens(agent: str, input_tokens: int):
    """Update session token count"""
    await db.execute(
        "UPDATE sessions SET input_tokens = ? WHERE agent = ?",
        (input_tokens, agent)
    )
    await db.commit()

# =============================================================================
# Session Persistence (Summary and Restore)
# =============================================================================

async def load_last_session(agent: str) -> Dict[str, Any]:
    """Load last session summary if available and recent"""
    summary_path = Path(str(LAST_SUMMARY_TEMPLATE).format(agent=agent))

    if not summary_path.exists():
        return {"status": "not_found"}

    # Check age
    mtime = summary_path.stat().st_mtime
    age_hours = (time.time() - mtime) / 3600

    if age_hours > 24:
        return {"status": "stale", "age_hours": age_hours}

    with open(summary_path) as f:
        summary = f.read()

    return {"status": "success", "summary": summary, "age_hours": age_hours}

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
    persona_dir = WORKSPACE_ROOT / "agents" / agent / "persona"
    if persona_dir.exists() and any(
        f.read_text().strip() for f in persona_dir.glob("*.md") if f.is_file()
    ):
        return ""

    onboarding_path = WORKSPACE_ROOT / "agents" / agent / "onboarding.md"
    if not onboarding_path.exists():
        return ""

    text = onboarding_path.read_text()
    substitutions = {
        "{{AGENT_NAME}}": agent,
        "{{OWNER_NAME}}": os.environ.get("OWNER_NAME", "User"),
        "{{SYSTEM_NAME}}": os.environ.get("SYSTEM_NAME", "karakos"),
    }
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    return text.strip()


async def start_agent_subprocess(agent: str):
    """Start persistent Claude subprocess for agent"""
    config = agent_config.get(agent, {})
    if not config:
        log.error(f"No config found for agent: {agent}")
        return

    session_id = await get_or_create_session(agent)
    system_prompt_path = WORKSPACE_ROOT / config.get("system_prompt", "")

    if not system_prompt_path.exists():
        log.error(f"System prompt not found for {agent}: {system_prompt_path}")
        return

    # The CLI's --system-prompt flag takes the prompt string, not a file
    # path. Read the file contents here.
    try:
        system_prompt_text = system_prompt_path.read_text()
    except Exception as e:
        log.error(f"Failed to read system prompt for {agent}: {e}")
        return

    # Load persona
    persona_content = load_persona_files(agent)

    # First-boot gate: if no persona has been written yet, prepend the
    # onboarding prompt so the agent asks the user for guidance instead
    # of arriving fully-formed.
    onboarding = load_onboarding_prompt(agent)
    if onboarding:
        log.info(f"Injecting onboarding prompt for {agent} (persona is empty)")
        persona_content = onboarding + ("\n\n" + persona_content if persona_content else "")

    # Load last session summary if available
    last_session = await load_last_session(agent)
    if last_session["status"] == "success":
        log.info(f"Injecting session summary for {agent} (age: {last_session['age_hours']:.1f}h)")
        persona_content = f"[SESSION RESET]\n\n{last_session['summary']}\n\n{persona_content}"

    # Build command
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--model", config.get("model", "sonnet"),
        "--max-turns", str(config.get("max_turns", 200)),
        "--verbose",
        "--dangerously-skip-permissions",
        "--session-id", session_id,
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
            log.info(f"{agent} permission policy: allow={allow_list} deny={deny_list}")
    else:
        log.warning(f"No settings file at {CLAUDE_SETTINGS_PATH}; starting {agent} with hooks unwired")

    if persona_content:
        cmd.extend(["--append-system-prompt", persona_content])

    # Add disallowed tools
    disallowed = config.get("disallowed_tools", [])
    for pattern in disallowed:
        cmd.extend(["--disallowedTools", pattern])

    # Add allowed tools if specified
    allowed = config.get("allowed_tools")
    if allowed:
        cmd.extend(["--allowedTools", ",".join(allowed)])

    # Per-agent environment (#99) — agents.json's `env` dict is layered onto
    # the server's own environment rather than replacing it, so the agent
    # still inherits API keys, WORKSPACE_ROOT, etc. Per-agent entries win on
    # collision, which is the point: an operator scoping one agent to a
    # different API base URL or timeout without touching every other agent.
    env_overrides = config.get("env") or {}
    # KARAKOS_AGENT is not an override — it is identity. The MCP tool server
    # runs as a child of this subprocess and otherwise has no way to say
    # which agent is calling `ask_user`, which is what decides where the
    # question is posted (#101). Set first so an operator's `env` block can
    # still win if they really mean to.
    spawn_env = {**os.environ, "KARAKOS_AGENT": agent, **env_overrides}
    if env_overrides:
        log.info(f"{agent} env overrides: {sorted(env_overrides.keys())}")

    log.info(f"Starting {agent} subprocess (model={config.get('model')}, session={session_id[:8]})")

    # Cancel any stderr reader left over from a prior subprocess for this
    # agent before spawning a new one — otherwise it leaks on every respawn.
    stale_reader = stderr_reader_tasks.pop(agent, None)
    if stale_reader and not stale_reader.done():
        stale_reader.cancel()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
        )
        agent_processes[agent] = proc
        agent_states[agent] = "IDLE"
        agent_sessions[agent] = session_id

        # Start stderr reader, tracked so it can be cancelled on kill/respawn.
        stderr_reader_tasks[agent] = asyncio.create_task(stderr_reader(agent, proc))

        log.info(f"{agent} subprocess started (PID {proc.pid})")
    except Exception as e:
        log.error(f"Failed to start {agent}: {e}")
        agent_states[agent] = "ERROR_RECOVERY"

async def stderr_reader(agent: str, proc: asyncio.subprocess.Process):
    """Read and log stderr from agent subprocess"""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode().strip()
            if msg:
                log.warning(f"{agent} stderr: {msg}")
    except Exception as e:
        log.error(f"stderr reader error for {agent}: {e}")

async def kill_agent_subprocess(agent: str):
    """Terminate agent subprocess"""
    proc = agent_processes.get(agent)
    if not proc:
        return

    log.info(f"Killing {agent} subprocess (PID {proc.pid})")
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.warning(f"{agent} didn't terminate, sending SIGKILL")
        proc.kill()
        await proc.wait()

    agent_processes.pop(agent, None)

    reader_task = stderr_reader_tasks.pop(agent, None)
    if reader_task and not reader_task.done():
        reader_task.cancel()

    log.info(f"{agent} subprocess terminated")

async def restart_agent(agent: str):
    """Restart agent subprocess"""
    log.info(f"Restarting {agent}")
    await kill_agent_subprocess(agent)
    await clear_session(agent)
    agent_last_cost.pop(agent, None)
    response_buffers[agent] = ""
    await start_agent_subprocess(agent)


async def reload_agent(agent: str):
    """Bounce the subprocess but keep the session — used to pick up new
    SYSTEM_PROMPT / persona / MCP config without dropping conversation
    context. The respawn calls --resume on the existing session_id.
    """
    log.info(f"Reloading {agent} (preserving session)")
    await kill_agent_subprocess(agent)
    agent_last_cost.pop(agent, None)
    response_buffers[agent] = ""
    await start_agent_subprocess(agent)


async def interrupt_agent(agent: str) -> bool:
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
    if agent_states.get(agent) != "PROCESSING":
        return False

    log.info(f"Interrupting {agent} (session preserved)")
    interrupted_agents.add(agent)
    await kill_agent_subprocess(agent)
    agent_last_cost.pop(agent, None)
    response_buffers[agent] = ""
    await start_agent_subprocess(agent)
    return True


async def flush_agent_queue(agent: str) -> int:
    """Drop every message still waiting for `agent`. Returns how many.

    In-progress messages are left alone: they are already inside the
    subprocess and deleting the row would only lose the record of them.
    """
    async with db.execute(
        "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
        (agent, STATUS_QUEUED),
    ) as cursor:
        row = await cursor.fetchone()
        pending = row["count"]

    if pending:
        await db.execute(
            "UPDATE message_queue SET processed = ? WHERE agent = ? AND processed = ?",
            (STATUS_SKIPPED, agent, STATUS_QUEUED),
        )
        await db.commit()

    log.info(f"Flushed {pending} queued message(s) for {agent}")
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

    # Calculate delta
    last_cost = agent_last_cost.get(agent, 0.0)
    cost_delta = session_total - last_cost
    agent_last_cost[agent] = session_total

    # Store in database
    await db.execute(
        """
        INSERT INTO cost_events (agent, cost_delta, session_total, input_tokens, output_tokens, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (agent, cost_delta, session_total, input_tokens, output_tokens, duration_ms)
    )
    await db.commit()

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

    status = info.get("status")
    resets_at = info.get("resetsAt")
    resets_at = int(resets_at) if isinstance(resets_at, (int, float)) else None

    async with db.execute(
        "SELECT alerted_for_resets_at FROM rate_limit_state WHERE agent = ?", (agent,)
    ) as cursor:
        prior = await cursor.fetchone()
    already_alerted = prior["alerted_for_resets_at"] if prior else None

    await db.execute(
        """
        INSERT INTO rate_limit_state
            (agent, status, rate_limit_type, resets_at, overage_status,
             is_using_overage, alerted_for_resets_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(agent) DO UPDATE SET
            status = excluded.status,
            rate_limit_type = excluded.rate_limit_type,
            resets_at = excluded.resets_at,
            overage_status = excluded.overage_status,
            is_using_overage = excluded.is_using_overage,
            updated_at = CURRENT_TIMESTAMP
        """,
        (agent, status, info.get("rateLimitType"), resets_at,
         info.get("overageStatus"), int(bool(info.get("isUsingOverage"))),
         already_alerted)
    )
    await db.commit()

    progress = rate_limit_window_progress(info, now=now)
    should_alert = (
        status in RATE_LIMIT_ALERT_STATUSES
        or (progress is not None and progress >= RATE_LIMIT_ALERT_FRACTION)
    )
    if not should_alert:
        return
    if resets_at is not None and already_alerted == resets_at:
        return  # already said so for this window

    await db.execute(
        "UPDATE rate_limit_state SET alerted_for_resets_at = ? WHERE agent = ?",
        (resets_at, agent)
    )
    await db.commit()

    consumed = "in the warning band" if progress is None else f"{progress * 100:.0f}% through the window"
    log.warning(f"{agent} rate-limit headroom low: status={status}, {consumed}")

    channel_id = RATE_LIMIT_ALERT_CHANNEL_ID
    if channel_id and channel_id != "0":
        await post_to_discord(
            agent, channel_id,
            f"⚠️ `{agent}` rate-limit headroom low — status `{status}`, {consumed}."
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

    Splits on the largest boundary that fits — paragraph, then line, then a
    hard cut mid-line. The hard cut is the part that matters: a reply with no
    blank line and no newline in it has no boundary to split on, and the
    previous implementation returned it as a single oversize chunk. Discord
    rejects anything over 2000 with a 400 and the message is lost.
    """
    if len(text) <= max_length:
        return [text] if text else []

    chunks: List[str] = []
    remaining = text

    while len(remaining) > max_length:
        window = remaining[:max_length]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            # A solid wall of text. Cut it at the limit rather than handing
            # Discord something it will refuse.
            cut = max_length
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks if chunks else [text]
def _write_dead_letter(agent: str, channel_id: str, content: str, reason: str,
                       attempts: int) -> None:
    """Record a reply that was generated but could not be delivered.

    The agent ran, the tokens were spent, the answer exists — and without this
    the only trace is a log line. Writing it somewhere durable is what makes it
    recoverable, and what lets /health say the delivery path is broken instead
    of everything looking idle and fine.

    Never raises: this is the error path, and a failure to record a failure
    must not take down the response loop on top of it.
    """
    try:
        DEAD_LETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "channel_id": channel_id,
            "reason": reason,
            "attempts": attempts,
            "content": content,
        }
        with open(DEAD_LETTER_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
        log.error(
            f"DEAD LETTER: {agent}'s reply for channel {channel_id} "
            f"({len(content)} chars) undelivered after {attempts} attempt(s): "
            f"{reason}. Written to {DEAD_LETTER_PATH}"
        )
    except Exception as e:
        log.error(f"Failed to write dead letter (reply is now lost): {e}")


def dead_letter_count() -> int:
    """How many replies are sitting undelivered. 0 if the file is absent."""
    try:
        if not DEAD_LETTER_PATH.exists():
            return 0
        with open(DEAD_LETTER_PATH) as f:
            return sum(1 for line in f if line.strip())
    except Exception as e:
        log.error(f"Could not count dead letters: {e}")
        return 0


async def post_to_discord(agent: str, channel_id: str, content: str,
                          reply_to: Optional[str] = None,
                          dead_letter: bool = False) -> Optional[str]:
    """Post message to Discord as agent, splitting if over 2000 chars.

    `dead_letter=True` marks this content as agent output worth preserving if
    delivery fails — a reply someone is waiting on. It is off by default so
    that incidentals (tool-event lines, cost updates, the crash notice) do not
    fill the queue with things nobody would replay.
    """
    global http_session

    # Skip posting if channel_id is "0" (silent mode)
    if channel_id == "0":
        return None

    # Get agent's Discord token, fallback to primary agent
    token = AGENT_TOKENS.get(agent)
    if not token:
        # Use first available token as fallback
        if AGENT_TOKENS:
            token = list(AGENT_TOKENS.values())[0]
            content = f"[{agent}] {content}"
        else:
            log.warning(f"No Discord tokens configured, cannot post for {agent}")
            return None

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json"
    }

    chunks = split_discord_message(content)
    last_msg_id = None
    failed = 0
    attempts_used = 0
    last_reason = "unknown"

    for idx, chunk in enumerate(chunks):
        payload = {"content": chunk}
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
                        last_reason = "rate limited"
                        await asyncio.sleep(retry_after)
                    elif resp.status in (401, 403, 404):
                        # Permission revoked, token rejected, channel gone. None
                        # of these clear by trying again — retrying only delays
                        # the moment the reply is recorded as undeliverable.
                        body = (await resp.text())[:200]
                        last_reason = f"HTTP {resp.status} ({body})"
                        log.error(
                            f"Discord API error {resp.status} on chunk "
                            f"{idx + 1}/{len(chunks)} ({len(chunk)} chars); "
                            f"not retryable"
                        )
                        break
                    else:
                        last_reason = f"HTTP {resp.status}"
                        log.error(
                            f"Discord API error {resp.status} on chunk "
                            f"{idx + 1}/{len(chunks)} ({len(chunk)} chars) "
                            f"(attempt {attempt}/{POST_MAX_ATTEMPTS}): "
                            f"{await resp.text()}"
                        )
                        if attempt < POST_MAX_ATTEMPTS:
                            await asyncio.sleep(POST_RETRY_BASE_SEC * attempt)
            except Exception as e:
                last_reason = f"{type(e).__name__}: {e}"
                log.error(
                    f"Error posting chunk {idx + 1}/{len(chunks)} to Discord "
                    f"(attempt {attempt}/{POST_MAX_ATTEMPTS}): {e}"
                )
                if attempt < POST_MAX_ATTEMPTS:
                    await asyncio.sleep(POST_RETRY_BASE_SEC * attempt)

        attempts_used = max(attempts_used, attempt)
        if not posted:
            failed += 1

    # A chunk that never landed is a piece of the reply the user will never
    # see. Returning the id of a sibling chunk reports the whole message as
    # delivered and the loss goes unnoticed — which is how two replies
    # vanished silently before this was caught.
    if failed:
        log.error(
            f"post_to_discord: {failed} of {len(chunks)} chunk(s) failed for "
            f"{agent} in {channel_id}; message is incomplete"
        )
        if dead_letter:
            _write_dead_letter(
                agent, channel_id, content,
                f"{failed} of {len(chunks)} chunk(s) failed: {last_reason}",
                attempts_used,
            )
        return None

    return last_msg_id

def gateway_agent() -> Optional[str]:
    """The agent whose bot token bin/relay.py logs in with.

    This matters for #101 and only for #101. A button click is delivered over
    the gateway to the application that posted the message, and the relay
    holds exactly one gateway connection — opened with the first agent in
    agents.json that has a token (bin/relay.py::main). A question posted
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
    proc = agent_processes.get(agent)
    if not proc or not proc.stdin:
        log.error(f"No subprocess for {agent}")
        return

    agent_states[agent] = "PROCESSING"
    write_agent_beacon(agent, "PROCESSING", force=True)
    response_buffers[agent] = ""

    # Send message — Claude Code stream-json input envelope.
    # Format: {"type": "user", "message": {"role": "user", "content": <str>}}
    # The bare {"type":"user","content":...} form is rejected by the SDK.
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    }) + "\n"
    try:
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        log.info(f"Sent message to {agent} ({len(message_ids)} queued messages)")
    except Exception as e:
        log.error(f"Error sending to {agent}: {e}")
        agent_states[agent] = "ERROR_RECOVERY"
        write_agent_beacon(agent, "ERROR_RECOVERY", force=True)

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
        await db.execute(
            f"UPDATE message_queue SET response = ? WHERE message_id IN ({placeholders})",
            (text, *message_ids),
        )
        await db.commit()
    except Exception as e:
        log.warning(f"streaming response write failed: {e}")


def extract_permission_denials(result_event: Dict) -> List[Dict]:
    """Pull the `permission_denials` list off a stream-json `result` event.
    Always a list, never None, so callers can iterate unconditionally."""
    return result_event.get("permission_denials") or []


async def read_agent_response(
    agent: str, channel_id: str, message_ids: Optional[List[str]] = None
) -> tuple[str, Dict]:
    """Read and process agent response stream"""
    proc = agent_processes.get(agent)
    if not proc or not proc.stdout:
        return "", {}

    config = agent_config.get(agent, {})
    tool_streaming = config.get("tool_streaming", False)
    stream_to_channel = config.get("stream_to_channel", False)
    msg_ids = message_ids or []

    final_text = ""
    metadata = {}
    last_posted_chunk = ""

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            # Every event is proof the turn is still moving. This is the whole
            # beacon: a SIGSTOPped claude emits nothing, readline() blocks
            # here, and the timestamp stops advancing while the state stays
            # PROCESSING — which is precisely the pair wedge-check.py looks
            # for. Throttled internally, so a chatty turn is cheap.
            write_agent_beacon(agent, "PROCESSING", message_id=msg_ids[0] if msg_ids else None)

            # One `system`/`init` event opens the stream, listing the tool
            # set the CLI actually resolved for this session. A tool named
            # in permissions.deny (#99) is dropped from this list entirely
            # rather than surfacing as a runtime denial — logging it here is
            # the only place a full-tool deny is ever visible after the
            # fact.
            if event_type == "system" and event.get("subtype") == "init":
                tools = event.get("tools")
                if tools is not None:
                    log.info(f"{agent} session tools ({len(tools)}): {tools}")

            # The CLI reports rate-limit headroom in-band, on the stream that
            # is already open. Recorded rather than polled — see
            # record_rate_limit_event. Wrapped because a bookkeeping failure
            # must never cost the agent's actual reply, which is still
            # arriving on this same loop.
            if event_type == "rate_limit_event":
                try:
                    await record_rate_limit_event(agent, event.get("rate_limit_info"))
                except Exception as e:
                    log.error(f"Failed to record rate limit event for {agent}: {e}")

            # Claude Code stream-json output: each turn emits one or more
            # `assistant` events with content blocks (thinking/text/tool_use),
            # then a single `result` event closes the turn.
            if event_type == "assistant":
                message = event.get("message", {}) or {}
                got_text = False
                for block in message.get("content", []) or []:
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text:
                            final_text += text
                            response_buffers[agent] = final_text
                            got_text = True
                            if stream_to_channel and channel_id != "0":
                                # TODO: Implement chunked streaming
                                pass
                    elif btype == "tool_use":
                        tool_name = block.get("name", "unknown")
                        log.info(f"{agent} called tool: {tool_name}")
                        if tool_streaming and channel_id != "0":
                            await post_to_discord(agent, channel_id, f"🔧 {tool_name}")
                    # `thinking` blocks are intentionally ignored here — they
                    # are stripped from the final text below as a belt-and-
                    # braces measure for any inline <thinking> tags.

                if got_text:
                    cleaned = THINKING_BLOCK_RE.sub("", final_text)
                    await write_streaming_response(msg_ids, cleaned)

            elif event_type == "result":
                # Extract metadata. Token counts live under `usage`,
                # cost/duration are top-level. Final text is in `result`
                # for success, or `error` field for failures.
                usage = event.get("usage", {}) or {}
                metadata = {
                    "session_id": event.get("session_id"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_cost_usd": event.get("total_cost_usd", 0.0),
                    "duration_ms": event.get("duration_ms", 0),
                    "is_error": event.get("is_error", False),
                }
                # If the assistant stream produced nothing, fall back to
                # the result's flat `result` string (success) or `error`.
                if not final_text:
                    final_text = event.get("result", "") or event.get("error", "")

                # A fine-grained deny rule (e.g. "Bash(curl:*)") lets the
                # tool stay in the session's list but declines the specific
                # call at request time — that shows up here, not in the
                # init event's tool list. Each denial is the acceptance
                # test's "logged" half; #99.
                denials = extract_permission_denials(event)
                metadata["permission_denials"] = denials
                for denial in denials:
                    log.warning(
                        f"{agent} permission denied: tool={denial.get('tool_name')} "
                        f"input={denial.get('tool_input')}"
                    )
                break

    except Exception as e:
        log.error(f"Error reading response from {agent}: {e}")

    # Strip any inline thinking blocks (defense in depth)
    final_text = THINKING_BLOCK_RE.sub("", final_text).strip()

    # A turn that /interrupt ended has no answer, only a fragment of one.
    # Returning it would post half a sentence to the channel and bill it as
    # the reply — so it is dropped here, at the single point every caller of
    # read_agent_response goes through.
    if agent in interrupted_agents:
        interrupted_agents.discard(agent)
        log.info(f"{agent} turn discarded (interrupted)")
        final_text, metadata = "", {}

    agent_states[agent] = "IDLE"
    write_agent_beacon(agent, "IDLE", force=True)
    return final_text, metadata

async def process_agent_queue(agent: str):
    """Process pending messages for agent"""
    lock = agent_locks.get(agent)
    if not lock:
        return

    async with lock:
        if agent_states.get(agent) != "IDLE":
            return

        # Get pending messages
        async with db.execute(
            """
            SELECT * FROM message_queue
            WHERE agent = ? AND processed = ?
            ORDER BY created_at ASC
            LIMIT 20
            """,
            (agent, STATUS_QUEUED)
        ) as cursor:
            messages = await cursor.fetchall()

        if not messages:
            return

        # Mark as in progress
        message_ids = [msg["message_id"] for msg in messages]
        await db.execute(
            f"""
            UPDATE message_queue
            SET processed = ?, processing_started_at = CURRENT_TIMESTAMP
            WHERE message_id IN ({','.join('?' * len(message_ids))})
            """,
            (STATUS_IN_PROGRESS, *message_ids)
        )
        await db.commit()

        # Format batch
        channel_id = messages[0]["channel_id"]
        formatted_parts = []
        for msg in messages:
            timestamp = msg["created_at"]
            author = msg["author"]
            content = msg["content"]
            part = f"[{timestamp}] {author}: {content}"
            attachment_lines = format_attachments(msg["attachments"])
            if attachment_lines:
                part = f"{part}\n{attachment_lines}"
            formatted_parts.append(part)

        formatted_content = "\n\n".join(formatted_parts)

        # Stamp the automated-traffic sentinel when every message in the
        # batch is bot-originated (poke.sh always sets is_bot=1 for system
        # pokes, heartbeats, and task-complete notifications). A batch with
        # even one human Discord message stays unmarked, so a human reply
        # riding along in the same batch still gets a fresh recall block.
        if all(msg["is_bot"] for msg in messages):
            formatted_content = f"{AUTOMATED_TRAFFIC_SENTINEL}\n{formatted_content}"

        # Record where this turn came from before the subprocess can act on
        # it. POST /ask has no request context of its own — the MCP tool that
        # calls it knows only the agent name — so the channel a question is
        # posted into and the people entitled to answer it both come from
        # here (#101).
        agent_turn_context[agent] = {
            "channel_id": channel_id,
            "author_ids": [msg["author_id"] for msg in messages if not msg["is_bot"]],
            "message_ids": message_ids,
        }

        # Start typing indicator
        await start_typing(agent, channel_id)

        # Send to agent
        await send_to_agent(agent, formatted_content, message_ids)

        # Read response
        try:
            response_text, metadata = await read_agent_response(agent, channel_id, message_ids)
        finally:
            # The turn is over: any question still on screen belongs to a
            # subprocess that has stopped waiting for it, and answering it
            # would feed a reply into a turn that no longer exists.
            ask_registry.discard_agent(agent)
            agent_turn_context.pop(agent, None)

        # Stop typing
        await stop_typing(channel_id)

        # Post cost update
        if metadata:
            await post_cost_update(agent, metadata)
            await update_session_tokens(agent, metadata.get("input_tokens", 0))

        # Post response to Discord
        discord_msg_id = None
        if response_text and channel_id != "0":
            discord_msg_id = await post_to_discord(agent, channel_id, response_text,
                                                   dead_letter=True)

        # Mark complete
        await db.execute(
            f"""
            UPDATE message_queue
            SET processed = ?, response = ?, discord_response_id = ?, processed_at = CURRENT_TIMESTAMP
            WHERE message_id IN ({','.join('?' * len(message_ids))})
            """,
            (STATUS_COMPLETE, response_text, discord_msg_id, *message_ids)
        )
        await db.commit()

        log.info(f"{agent} processed {len(message_ids)} messages")

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

    # Retry posting messages that completed but weren't posted
    async with db.execute(
        "SELECT * FROM message_queue WHERE processed = ? AND discord_response_id IS NULL AND channel_id != '0'",
        (STATUS_COMPLETE,)
    ) as cursor:
        unposted = await cursor.fetchall()

    if unposted:
        log.warning(f"Found {len(unposted)} unposted responses, retrying")
        for msg in unposted:
            if msg["response"]:
                # Deliberately no dead_letter=True. These rows are already
                # durable in the queue and are retried on every startup, so
                # dead-lettering them would append a fresh copy of the same
                # reply each time the server came up against a channel that is
                # still unreachable.
                discord_id = await post_to_discord(msg["agent"], msg["channel_id"], msg["response"])
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

    if not agent or agent not in agent_config:
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
    except Exception as e:
        log.error(f"Error inserting message: {e}")
        return web.json_response({"error": "Database error"}, status=500)

    # Trigger processing if agent is idle
    if agent_states.get(agent) == "IDLE":
        asyncio.create_task(process_agent_queue(agent))

    return web.json_response({"status": "queued", "message_id": message_id}, status=202)

async def handle_health(request):
    """GET /health - Health check"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent_status = {}
    for agent in agent_config:
        proc = agent_processes.get(agent)
        queue_depth = 0
        async with db.execute(
            "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
            (agent, STATUS_QUEUED)
        ) as cursor:
            row = await cursor.fetchone()
            queue_depth = row["count"]

        agent_status[agent] = {
            "state": agent_states.get(agent, "UNKNOWN"),
            "alive": proc is not None and proc.returncode is None,
            "queue_depth": queue_depth,
            "session_id": agent_sessions.get(agent, "")[:8]
        }

    # A non-zero count means replies were generated and never delivered. It is
    # reported here because that failure is otherwise invisible — every agent
    # looks idle and healthy while its answers are going nowhere.
    undelivered = dead_letter_count()

    return web.json_response({
        "status": "healthy",
        "agents": agent_status,
        "dead_letters": undelivered,
        "dead_letter_path": str(DEAD_LETTER_PATH),
    })

async def handle_agents(request):
    """GET /agents - List agents"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agents_list = []
    for agent, config in agent_config.items():
        agents_list.append({
            "name": agent,
            "model": config.get("model"),
            # The same defaults the subprocess is actually launched with (see
            # start_agent). Reporting the raw config.get() would show a blank
            # for every agent that relies on the default, which reads as "not
            # configured" rather than "configured by omission".
            "max_turns": config.get("max_turns", 200),
            "timeout": config.get("timeout"),
            "state": agent_states.get(agent, "UNKNOWN"),
            "has_discord_token": agent in AGENT_TOKENS
        })

    return web.json_response({"agents": agents_list})

async def handle_agent_reset(request):
    """POST /agents/{name}/reset - Reset agent session"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    await restart_agent(agent)
    return web.json_response({"status": "reset"})


async def handle_agent_reload(request):
    """POST /agents/{name}/reload - Bounce subprocess, preserve session."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    await reload_agent(agent)
    return web.json_response({"status": "reloaded"})


async def handle_agent_interrupt(request):
    """POST /agents/{name}/interrupt - Stop the current generation."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    interrupted = await interrupt_agent(agent)
    # 200 either way: "it was already idle" is a successful answer to
    # "stop what you are doing", and the relay says which one happened.
    return web.json_response({
        "status": "interrupted" if interrupted else "idle",
        "interrupted": interrupted,
    })


async def handle_agent_kill(request):
    """POST /agents/{name}/kill - Kill the subprocess without respawning it."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    was_running = agent in agent_processes
    await kill_agent_subprocess(agent)
    return web.json_response({"status": "killed", "was_running": was_running})


async def handle_agent_flush(request):
    """POST /agents/{name}/flush - Drop the agent's pending message queue."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    flushed = await flush_agent_queue(agent)
    return web.json_response({"status": "flushed", "flushed": flushed})


# Agent name validator — same surface as bin/create-agent.sh's check, used
# to reject path traversal / shell metachars before we touch disk.
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


async def handle_agent_register(request):
    """POST /agents/{name}/register - Hot-load a newly-created agent.

    bin/create-agent.sh writes the new agent into config/agents.json and
    then POSTs here so the running server picks it up without a full
    restart. This endpoint:
      1. re-reads agents.json (and channels.json) via load_config()
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

    if agent in agent_processes:
        return web.json_response(
            {"error": "Agent already running", "agent": agent},
            status=409,
        )

    # Re-read agents.json + channels.json so the new entry, its Discord
    # token mapping, and any new channel routing all become visible to
    # the running server.
    await load_config()

    if agent not in agent_config:
        return web.json_response(
            {
                "error": (
                    f"Agent '{agent}' not found in config after reload — "
                    "verify it was written to config/agents.json"
                )
            },
            status=404,
        )

    log.info(f"Hot-registering new agent: {agent}")
    await start_agent_subprocess(agent)

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
    agent = data.get("agent")
    cost_delta = data.get("cost_delta", 0.0)

    if agent not in agent_config:
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

    async with db.execute("SELECT * FROM rate_limit_state") as cursor:
        rows = {row["agent"]: row for row in await cursor.fetchall()}

    agents = {}
    for name in agent_config:
        row = rows.get(name)
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

    return web.json_response({"agents": agents})


async def handle_cost_get(request):
    """GET /cost/{agent} - Get cost summary"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("agent")

    # Daily cost
    async with db.execute(
        """
        SELECT SUM(cost_delta) as total
        FROM cost_events
        WHERE agent = ? AND timestamp > datetime('now', '-1 day')
        """,
        (agent,)
    ) as cursor:
        row = await cursor.fetchone()
        daily = row["total"] or 0.0

    # Monthly cost
    async with db.execute(
        """
        SELECT SUM(cost_delta) as total
        FROM cost_events
        WHERE agent = ? AND timestamp > datetime('now', '-30 days')
        """,
        (agent,)
    ) as cursor:
        row = await cursor.fetchone()
        monthly = row["total"] or 0.0

    return web.json_response({
        "agent": agent,
        "daily": daily,
        "monthly": monthly,
        "session": agent_last_cost.get(agent, 0.0)
    })

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

    agent = data.get("agent")
    if not agent or agent not in agent_config:
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

    # Wait for agents to finish (max 30s)
    log.info("Waiting for agents to finish current messages...")
    for i in range(30):
        all_idle = all(agent_states.get(a) == "IDLE" for a in agent_config)
        if all_idle:
            break
        await asyncio.sleep(1)

    # Generate summaries for active agents
    log.info("Finalizing sessions...")
    for agent in agent_config:
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", str(Path(__file__).parent / "summarize-session.py"), agent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25)
            if proc.returncode == 0:
                log.info(f"Session summary generated for {agent}")
            else:
                log.warning(f"Session summary failed for {agent}: {stderr.decode()[:200]}")
        except asyncio.TimeoutError:
            log.warning(f"Session summary timed out for {agent}")
        except Exception as e:
            log.warning(f"Session summary error for {agent}: {e}")

    # Kill subprocesses
    log.info("Terminating agent subprocesses...")
    for agent in list(agent_processes.keys()):
        await kill_agent_subprocess(agent)

    # Close DB
    if db:
        await db.close()

    # Close HTTP session
    if http_session:
        await http_session.close()

    log.info("Shutdown complete")
    sys.exit(0)

# =============================================================================
# Server Startup
# =============================================================================

async def startup(app):
    """Initialize server on startup"""
    global http_session

    log.info("Starting Karakos Agent Server")

    # Initialize HTTP session
    http_session = aiohttp.ClientSession()

    # Initialize database
    await init_db()

    # Load configuration
    await load_config()

    # Initialize locks and state
    for agent in agent_config:
        agent_locks[agent] = asyncio.Lock()
        agent_states[agent] = "IDLE"
        response_buffers[agent] = ""
        # Overwrite any beacon left behind by a previous process. A crash
        # mid-turn leaves one reading PROCESSING with a timestamp that will
        # never advance again — which is indistinguishable from a live wedge,
        # so without this every restart-after-crash pages forever about an
        # agent that is now fine.
        write_agent_beacon(agent, "IDLE", force=True)

    # Crash recovery
    await crash_recovery()

    # Start agent subprocesses
    for agent in agent_config:
        await start_agent_subprocess(agent)

    # Register signal handlers in event loop context
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(graceful_shutdown("SIGTERM")))
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(graceful_shutdown("SIGINT")))

    log.info(f"Agent server ready on port {PORT}")

async def shutdown(app):
    """Cleanup on shutdown"""
    log.info("Server shutdown initiated")

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
    app.router.add_get("/health", handle_health)
    app.router.add_get("/agents", handle_agents)
    app.router.add_post("/agents/{name}/reset", handle_agent_reset)
    app.router.add_post("/agents/{name}/reload", handle_agent_reload)
    app.router.add_post("/agents/{name}/register", handle_agent_register)
    app.router.add_post("/agents/{name}/interrupt", handle_agent_interrupt)
    app.router.add_post("/agents/{name}/kill", handle_agent_kill)
    app.router.add_post("/agents/{name}/flush", handle_agent_flush)
    app.router.add_post("/cost", handle_cost)
    app.router.add_get("/cost/{agent}", handle_cost_get)
    app.router.add_get("/usage", handle_usage)
    app.router.add_post("/ask", handle_ask_create)
    app.router.add_get("/ask/{ask_id}", handle_ask_status)
    app.router.add_post("/ask/{ask_id}/answer", handle_ask_answer)

    # Register startup/shutdown handlers
    if with_lifecycle:
        app.on_startup.append(startup)
        app.on_shutdown.append(shutdown)

    return app


def main():
    """Main entry point"""
    # Signal handlers will be registered after event loop starts (in startup)
    # For now, just set flag to handle in asyncio context
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)

if __name__ == "__main__":
    main()
