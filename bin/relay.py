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
import json
import logging
import os
import re
import subprocess
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
from logging.handlers import RotatingFileHandler

# =============================================================================
# Utilities
# =============================================================================

def split_discord_message(text: str, max_length: int = 2000) -> List[str]:
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


# =============================================================================
# System commands
# =============================================================================

# Commands the relay handles itself instead of forwarding to an agent. Kept
# deliberately small: these three are the ones you need when an agent is
# already too wedged to read its own messages, which is exactly when shell
# access is least convenient.
SYS_COMMANDS = frozenset({"clear", "reload", "status"})

# <@123>, <@!123> (nickname form), <@&123> (role).
_MENTION_RE = re.compile(r"<@[!&]?\d+>")


def strip_mentions(text: str) -> str:
    """Drop Discord mention tokens so `@Agent /sys clear` matches as a command."""
    return _MENTION_RE.sub(" ", text or "").strip()


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
# Configuration
# =============================================================================

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.json"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
MESSAGES_DIR = WORKSPACE_ROOT / "data" / "messages"
HEALTH_FILE = WORKSPACE_ROOT / "data" / "health" / "relay.json"

AGENT_SERVER_PORT = os.environ.get("AGENT_SERVER_PORT", "18791")
AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", f"http://localhost:{AGENT_SERVER_PORT}")
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
OWNER_DISCORD_ID = int(os.environ.get("OWNER_DISCORD_ID", "0"))

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
discord_id_to_agent: Dict[int, str] = {}
active_dispatches: Dict[str, asyncio.Task] = {}
dispatch_semaphores: Dict[str, asyncio.Semaphore] = {}

# =============================================================================
# Configuration Loading
# =============================================================================

def load_config():
    """Load agent and channel configuration"""
    global agent_config, channels_config, discord_id_to_agent

    # Load agents
    if AGENTS_CONFIG_PATH.exists():
        with open(AGENTS_CONFIG_PATH) as f:
            config_data = json.load(f)
            agent_config = config_data.get("agents", {})
    else:
        agent_config = {}
        log.warning(f"Agents config not found: {AGENTS_CONFIG_PATH}")

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

    log.info(f"Loaded config for {len(agent_config)} agents, {len(channels_config.get('channels', {}))} channels")


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

    async def on_message(self, message: discord.Message):
        """Route Discord message to agent"""
        # Ignore own messages
        if message.author == self.user:
            return

        # Ignore messages from servers we aren't configured for
        if message.guild and str(message.guild.id) not in self.server_ids:
            return

        # Capture message
        await self.capture_message(message)

        # Determine target agent
        target_agent = None

        # Check for bot mention
        for mention in message.mentions:
            if mention.bot and mention.id in discord_id_to_agent:
                target_agent = discord_id_to_agent[mention.id]
                break

        # Fall back to channel default agent
        channel_name = self.get_channel_name(str(message.channel.id))
        channel_default = None
        if channel_name:
            channel_config = channels_config.get("channels", {}).get(channel_name, {})
            channel_default = channel_config.get("default_agent")

        # System commands run here, not in the agent. An agent that has stopped
        # reading its queue cannot process its own `/clear`, and that is the
        # case the command exists for — so this intercept sits ahead of routing
        # and never reaches send_to_agent_server.
        parsed = parse_sys_command(message.content)
        if parsed:
            await self.handle_sys_command(message, parsed[0], parsed[1],
                                          target_agent, channel_default)
            return

        if not target_agent:
            target_agent = channel_default

        if not target_agent:
            return  # No routing

        # Send to agent server
        await self.send_to_agent_server(message, target_agent)

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

        if cmd == "status":
            await self.sys_status(message)
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

    async def agent_server_post(self, path: str):
        """POST to the agent server. Returns (ok, detail-on-failure)."""
        try:
            async with self.http_session.post(
                f"{AGENT_SERVER_URL}{path}",
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status == 200:
                    return True, ""
                body = await resp.text()
                return False, f"agent server returned {resp.status}: {body[:200]}"
        except Exception as e:
            return False, str(e)

    def get_channel_name(self, channel_id: str) -> Optional[str]:
        """Get channel name from ID"""
        for name, config in channels_config.get("channels", {}).items():
            if config.get("id") == channel_id:
                return name
        return None

    async def send_to_agent_server(self, message: discord.Message, agent: str):
        """Send message to agent server"""
        channel_name = self.get_channel_name(str(message.channel.id))
        if not channel_name:
            channel_name = "unknown"

        payload = {
            "agent": agent,
            "channel": channel_name,
            "channel_id": str(message.channel.id),
            "server": "discord",
            "author": message.author.display_name,
            "author_id": str(message.author.id),
            "is_bot": message.author.bot,
            "content": message.content,
            "message_id": str(message.id),
            "mentions_agent": any(m.id in discord_id_to_agent for m in message.mentions)
        }

        try:
            async with self.http_session.post(
                f"{AGENT_SERVER_URL}/message",
                json=payload,
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status == 202:
                    log.info(f"Queued message for {agent} from {message.author.display_name}")
                else:
                    text = await resp.text()
                    log.error(f"Agent server error {resp.status}: {text}")
        except Exception as e:
            log.error(f"Error sending to agent server: {e}")

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
            "message_id": str(message.id)
        }

        # Write to daily JSONL
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_file = MESSAGES_DIR / f"messages-{date_str}.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def write_health_heartbeat(self):
        """Write health heartbeat"""
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_FILE, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "status": "healthy"
            }, f)

    async def close(self):
        """Cleanup on shutdown"""
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

    # Start dispatch adapter
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
        await dispatch.stop()
        log.info("Relay shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
