# Architecture

Technical reference for the Karakos system.

## Overview

```
┌──────────────────────────────────────────────┐
│                   Docker Container           │
│                                              │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Agent      │  │ Relay    │  │ Dashboard│ │
│  │ Server     │  │          │  │ (Next.js)│ │
│  │ :18791     │  │          │  │ :3000    │ │
│  └─────┬──────┘  └────┬─────┘  └────┬─────┘ │
│        │              │              │       │
│  ┌─────┴──────────────┴──────────────┴─────┐ │
│  │              SQLite + JSONL              │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Scheduler│  │ MCP Tool │  │ Skills    │  │
│  │          │  │ Server   │  │           │  │
│  └──────────┘  └──────────┘  └───────────┘  │
│                                              │
│  supervisord (PID 1: tini)                   │
└──────────────────────────────────────────────┘
         │                    │
    Discord API         Anthropic API
```

## Agent Server (`bin/agent-server.py`)

The core process. Manages everything:

### Subprocess Management
- Launches Claude CLI as child processes via `asyncio.create_subprocess_exec`
- One subprocess per agent (primary, relay, plus any dynamic agents)
- Communicates via stdin/stdout using `--output-format stream-json`
- Monitors health, restarts on crash, notifies on failure

### Message Queue
- SQLite table `message_queue` with processing states:
  - `0` = queued
  - `1` = in-progress
  - `2` = complete
  - `3` = crashed
  - `4` = skipped (duplicate, rate-limited)
- Messages come from: Discord (via relay), dashboard (via API), poke.sh (inter-agent)

### HTTP API
All endpoints require `Authorization: Bearer {AGENT_SERVER_TOKEN}`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | System health, agent states, queue depth |
| `/agents` | GET | Agent list with status, model, cost |
| `/agents/{name}/register` | POST | Hot-register new agent |
| `/agents/{name}/reset` | POST | Reset agent session |
| `/message` | POST | Queue a message for an agent |
| `/message/{id}/status` | GET | Check message processing status |
| `/cost/{agent}` | GET | Cost breakdown (daily/monthly) |
| `/restart/server` | POST | Graceful server restart |
| `/restart/relay` | POST | Restart relay subprocess |

### Cost Tracking
- Tracks API spend per agent per session
- Enforces daily/monthly limits at the `/message` endpoint
- Owner messages bypass cost limits
- Posts cost events to #signals

## Relay (`bin/relay.py`)

Routes Discord messages to agents and dispatches work:

### Adapters
- **DiscordAdapter**: Connects to Discord, routes messages to agent server
- **DispatchAdapter**: Watches inbox directories for work briefs, invokes builder/reviewer
- **CaptureAdapter**: Persists all messages to JSONL logs

### Message Flow
```
Discord message → Relay → Agent Server /message → Agent subprocess
                                                      ↓
Discord ← Agent Server (posts response) ← Agent response
```

## Scheduler (`bin/scheduler.py`)

Replaces cron. Runs periodic tasks with full environment:

| Task | Interval | Script |
|------|----------|--------|
| Heartbeat | 30 min | `bin/heartbeat.sh` → `bin/poke.sh` |
| Health check | 10 min | `bin/health-monitor.py` |
| Memory maintenance | 6 hours | `bin/memory-maintenance.py` |
| Data purge | 24 hours | Removes old JSONL/logs beyond retention |
| Update check | Weekly | `bin/check-updates.sh` |

The loop ticks every 15 seconds (`SCHEDULER_TICK_SECONDS`), because it also
drives the oneshot spool below.

## Scheduled one-off work (`bin/oneshot.py`)

The table above is fixed at build time. `bin/oneshot.py` is the primitive that
lets an agent schedule *arbitrary* future work at runtime — so "I'll check back
in ten minutes" is a mechanism rather than a sentence. Agents reach it through
the `schedule` MCP tool; humans and scripts through the CLI.

```
oneshot.py schedule --label check-logs --when 10m --message "check the logs"
oneshot.py list
oneshot.py cancel check-logs
```

Each scheduled item is one JSON file in `data/oneshot-spool/`, holding the
**absolute** epoch second it is due — never the relative span the caller typed.
`data/` is a persistent volume, so the spool outlives the container that wrote
it, and `scheduler.py` re-reads it at startup before entering its loop.

There is no systemd inside this image, so there are no transient timers to
re-arm: being in the spool *is* being armed, and the scheduler tick is what
fires it. A deadline that passed while the container was down fires
immediately, unless it is more than `ONESHOT_STALE_AFTER_SECONDS` late
(default 24h), in which case it is dropped with a log line rather than
arriving days after it was useful.

## MCP Tool Server (`mcp/tools-server.py`)

JSON-RPC 2.0 server over stdin/stdout. Provides tools to Claude agents:

### Core Tools
- `workspace` — System config, agent registry
- `session` — Finalize/load session summaries
- `memory` — Query episodic memory and facts
- `schedule` — Schedule/list/cancel future work (see `bin/oneshot.py` above)
- `discord` — Read-only Discord access (channels, history)
- `taskboard` — Task tracking
- `vault` — Git-backed knowledge store

### Skill Discovery
Scans `skills/*/tools.json` at startup. Each skill provides tool definitions and implementation scripts. Tools are dispatched to skill scripts via subprocess with `TOOL_ARGS` environment variable.

### Audit Trail
All tool calls logged to SQLite (`data/mcp-tools-audit.db`) with timestamp, tool name, duration, success/failure.

## Dashboard

Next.js app providing browser-based access:

| Page | Description |
|------|-------------|
| `/` | Home — agent status cards, uptime, queue depth |
| `/agents` | Agent detail — status, cost, model, session reset |
| `/conversations` | Message feed with channel/human/tool-use filters |
| `/chat` | Direct chat with agents (SSE streaming) |
| `/system` | Server health, component status |
| `/settings` | Configuration viewer |

### Authentication
- HTTP Basic Auth (username: `admin`, password from setup)
- Session cookie (httpOnly, 24-hour expiry)
- All API routes verify session before proxying to agent server

### Chat Flow
```
Browser → POST /api/chat → Agent Server /message (channel_id="0")
Browser ← SSE /api/chat (polls /message/{id}/status for streaming)
```
Channel ID `"0"` means responses stay in the dashboard — not posted to Discord.

## Session Persistence

At 85% context budget:
1. `summarize-session.py` generates a summary via Claude API call
2. Summary saved to `data/last-session-summary-{agent}.md`
3. Agent subprocess resets
4. Summary re-injected on next session start

## Memory System

### Episodes
- Captured from conversations
- Importance scores decay over time
- Consolidated when short/redundant

### Facts
- Extracted from episodes
- Stored with subject, confidence, domain
- Injected as context on session start

### Embeddings
- Generated via fastembed (BAAI/bge-small-en-v1.5)
- Stored as numpy float32 blobs in SQLite
- Used for semantic search in `memory.recall`

## Protected Paths

Two-tier git hook enforcement:

- **Tier 1** (hard block): `system/`, `config/`, `bin/agent-server.py`, `bin/relay.py`, `Dockerfile`
- **Tier 2** (warn): `bin/scheduler.py`, `bin/entrypoint.sh`, `mcp/tools-server.py`

Builder agents can't commit to Tier 1 paths. Changes require PR review by the owner.

## Data Layout

```
data/
├── messages/          # JSONL logs (daily rotation)
│   └── messages-2026-03-30.jsonl
├── memory/
│   └── memory.db      # Episodes, facts, embeddings
├── health/            # Component heartbeat files
│   ├── mcp-tools.json
│   └── memory-maintenance.json
├── agent-server.db    # Message queue, sessions, cost
├── mcp-tools-audit.db # Tool call audit trail
└── taskboard.json     # Task tracking
```
