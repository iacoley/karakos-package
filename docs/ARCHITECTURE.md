# Architecture

Technical reference for the Karakos system. If you only read one section, read
[The two message paths](#the-two-message-paths) — most confusion about this
system comes from assuming replies travel back the way they came in. They
don't.

- [The shape of it](#the-shape-of-it)
- [The two message paths](#the-two-message-paths)
- [Agent server](#agent-server-binagent-serverpy)
- [Relay](#relay-binrelaypy)
- [Scheduler](#scheduler-binschedulerpy)
- [Scheduled one-off work](#scheduled-one-off-work-binoneshotpy)
- [MCP tool servers](#mcp-tool-servers)
- [Dashboard](#dashboard)
- [Agent lifecycle](#agent-lifecycle)
- [Agents and the registry](#agents-and-the-registry)
- [The turn loop](#the-turn-loop)
- [Queue and sessions](#queue-and-sessions)
- [Shards](#shards)
- [The hive](#the-hive)
- [Prompt composition](#prompt-composition)
- [Guard rails](#guard-rails)
- [Memory](#memory)
- [Comms](#comms)
- [Monitoring](#monitoring)
- [Build queue](#build-queue)
- [Credentials](#credentials)
- [Protected paths](#protected-paths)
- [Data layout](#data-layout)
- [The upgrade model](#the-upgrade-model)
- [Known gaps](#known-gaps)
- [Where each 2.0 feature lives](#where-each-20-feature-lives)

## The shape of it

Everything is **one container**. `tini` is PID 1, it execs `bin/entrypoint.sh`,
and that execs `supervisord`, which starts four long-lived programs. There is
no service mesh and no second container to coordinate with.

```
┌─ container ────────────────────────────────────────────────────────────┐
│  tini (PID 1) → entrypoint.sh → supervisord                            │
│                                                                        │
│   ┌──────────────┐   ┌──────────────┐   ┌───────────┐  ┌────────────┐  │
│   │ agent-server │   │   relay.py   │   │ scheduler │  │ dashboard  │  │
│   │ .py  :18791  │   │              │   │   .py     │  │ next :3000 │  │
│   └──────┬───────┘   └──────┬───────┘   └─────┬─────┘  └──────┬─────┘  │
│          │                  │                 │               │        │
│          │ spawns one per agent               │ pokes         │        │
│          ▼                                    ▼               │        │
│   ┌──────────────┐                     ┌────────────┐         │        │
│   │  claude CLI  │  stdin/stdout       │ bin/*.sh   │         │        │
│   │  stream-json │  ◀───────────────▶  │ bin/*.py   │         │        │
│   └──────┬───────┘                     └────────────┘         │        │
│          │ MCP over stdio                                     │        │
│          ▼                                                    │        │
│   ┌───────────────────────────┐                               │        │
│   │ mcp/tools-server.py       │  ← skills/<name>/tools.json   │        │
│   │ mcp/admin-server.py       │                               │        │
│   └───────────────────────────┘                               │        │
│                                                               │        │
│   ┌───────────────────────────────────────────────────────────▼─────┐  │
│   │  data/ (volume)   logs/ (volume)   inbox/ (volume)              │  │
│   │  config/ agents/ .karakos/  ← bind-mounted from the host        │  │
│   └─────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
        ▲                    ▲                              ▲
   Discord gateway      Discord REST                  Anthropic API
   (relay holds the     (agent-server posts           (via the claude
    one websocket)       replies directly)             CLI subprocess)
```

Supervised programs, from `config/supervisord.conf` — all four `autostart` and
`autorestart`, with **no `priority` set**, so there is no guaranteed start
order:

| Program | Command | `stopwaitsecs` |
|---|---|---|
| `agent-server` | `python3 /workspace/bin/agent-server.py` | 45 |
| `relay` | `python3 /workspace/bin/relay.py` | 10 |
| `dashboard` | `npx next start -p ${DASHBOARD_PORT:-3000}` | 10 |
| `scheduler` | `python3 /workspace/bin/scheduler.py` | 5 |

### Ports

The Dockerfile declares no `EXPOSE`; `config/docker-compose.yml` publishes two:

| Port | Published as | Notes |
|---|---|---|
| `${DASHBOARD_PORT:-3000}` | all host interfaces | The web UI |
| `${AGENT_SERVER_PORT:-18791}` | `127.0.0.1` only | The HTTP API. It binds `0.0.0.0` *inside* the container; the port map is what keeps it off your LAN |

Every agent-server endpoint requires `Authorization: Bearer $AGENT_SERVER_TOKEN`.

### What survives a restart

| Path | Kind |
|---|---|
| `config/`, `agents/`, `.karakos/` | Bind-mounted from your checkout |
| `data/`, `logs/`, `inbox/` | Named Docker volumes |
| `~/.claude`, `~/.claude.json` | Bind-mounted host credentials, read-write so token refresh persists |

Everything else — `bin/`, `mcp/`, `skills/`, `system/`, the built dashboard —
is baked into the image and replaced on upgrade. The container health check and
supervisord's `autorestart` are the only watchdogs for the supervised programs:
there is no host-side watchdog for a dead scheduler (a post-2.0 item).

## The two message paths

**Discord in is not Discord out.** The relay holds the single gateway
websocket and carries messages *in*. Replies go out over the Discord REST API
straight from the agent server, using that agent's own bot token. The relay
never sees them.

```
IN                                      OUT
Discord ──▶ relay.py                    agent-server ──▶ Discord REST API
              │  POST /message                ▲
              ▼                               │  the same process that
        agent-server ──▶ claude ──────────────┘  read the answer posts it
```

This is why an agent can go quiet in Discord while the dashboard still shows
it working, and why button clicks for `ask_user` come back through the relay
(only it has a gateway connection) while the question itself is posted by the
agent server.

Inbound, in order (`DiscordAdapter.on_message`):

1. The bot's own posts feed the reply gate and stop there.
2. Guilds absent from `config/channels.json` are dropped.
3. The message is captured to `data/messages/messages-YYYY-MM-DD.jsonl`.
4. The target agent is resolved from a bot mention, else the channel's
   `default_agent`.
5. `/clear`, `/reload`, `/status` and `/usage` are handled **inside the relay**
   and never reach an agent.
6. Bot authors pass a guest budget (12 turns by default); humans pass the
   reply gate if the channel sets one.
7. Attachments download (≤25 MB each, ≤10 per message), then `POST /message`.
   On 429, 5xx or a connection failure the payload spools to
   `data/deferred-messages/` and the scheduler retries it every 5 minutes.

Server side, `POST /message` returns **202 immediately** — it queues, it does
not wait. A duplicate `message_id` also returns 202, so a retry is safe. Then
`process_agent_queue` takes the agent's lock, drains up to 20 queued messages
into one turn, writes a single line of stream-json to the subprocess's stdin,
and reads events back off stdout: assistant text accumulates into the queue
row as it arrives, tool events drive the dashboard activity pill and throttled
Discord tool lines (at most 12 a turn, ≥5s apart), and a `result` event closes
the turn with cost and token counts. The reply is posted, the row is marked
complete, and the queue is re-checked before the lock is released.

## Agent server (`bin/agent-server.py`)

The core. Owns the queue, the money, and the Claude subprocesses.

### Subprocess management

One persistent `claude` CLI child per agent, spawned at startup and kept alive:

```
claude -p --input-format stream-json --output-format stream-json
       --model <config.model> --max-turns <config.max_turns|200> --verbose
       --dangerously-skip-permissions --session-id <uuid>
       --system-prompt <text> --settings config/claude-settings.json
       [--append-system-prompt <persona>] [--allowedTools/--disallowedTools]
```

A `respawn_watcher` restarts a subprocess that exits unexpectedly, capped at
**3 respawns per 5 minutes**; past that the agent is left down and a notice is
posted rather than flapping silently.

### Message queue

SQLite table `message_queue` in `data/memory/agent-server.db`:

| `processed` | Meaning |
|---|---|
| `0` | Queued |
| `1` | In progress |
| `2` | Complete |
| `3` | Crashed |
| `4` | Skipped (duplicate, rate-limited, flushed) |

Depth is capped at 50 per agent. Messages arrive from Discord (relay), the
dashboard, `bin/kara`, and `bin/poke.sh`.

On startup, `crash_recovery()` re-marks rows stuck at `1` as `3`, and re-posts
any reply that completed but never made it to Discord.

### HTTP API

All endpoints require the bearer token.

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | System health, agent states, queue depth |
| `/agents` | GET | Agent list with status, model, cost |
| `/message` | POST | Queue a message — returns 202, does not wait |
| `/agents/{name}/reset` | POST | New session; context destroyed |
| `/agents/{name}/reload` | POST | Respawn on the same session; context kept |
| `/agents/{name}/interrupt` | POST | Stop an in-flight turn, discard the partial |
| `/agents/{name}/kill` | POST | Stop and stay down |
| `/agents/{name}/flush` | POST | Drop that agent's queued messages |
| `/agents/{name}/queue` | GET | Inspect that agent's pending messages |
| `/agents/{name}/queue/{id}` | DELETE | Cancel one queued message; refuses one already in flight |
| `/agents/{name}/register` | POST | Hot-register a newly created agent |
| `/cost` | POST | Record a cost event |
| `/cost` | GET | Cost across all agents |
| `/cost/{agent}` | GET | Daily and monthly breakdown for one agent |
| `/cost/conversations` | GET | Cost attributed per conversation |
| `/usage` | GET | Rate-limit and token usage |
| `/ask` | POST | Raise an `ask_user` question |
| `/ask/{id}` | GET | Poll its state |
| `/ask/{id}/answer` | POST | Deliver the clicked answer |

There is **no `/status` and no `/message/{id}/status`**. `GET /health` carries
uptime and total queue depth; `GET /agents` carries the per-agent detail. A
test in `tests/test_agent_server_routes.py` parses every `agentFetch()` call in
the dashboard and fails if one names a path and method this table does not
register, so a client calling a 404 is now a red build rather than a card
rendering zeroes.

### Cost control

Every turn's spend is written to `cost_events` per agent. `COST_DAILY_LIMIT`
(default 25.00) and `COST_MONTHLY_LIMIT` (default 500.00) are checked when a
message is **queued**, not when it completes, and `COST_WARNING_THRESHOLD`
(0.75) posts a warning to the signals channel before the cap bites. Messages
from the owner bypass the limits. A separate check warns when the Anthropic
rate-limit headroom runs low.

### Steering (mid-turn messages)

A message for a shard that is mid-turn is written to the CLI's stdin at once
instead of waiting for the turn to end. The CLI queues the line: at a tool
boundary it joins the running turn (one `result`), otherwise it starts the next
turn by itself. The server spawns `claude` with `--replay-user-messages`, keeps a
per-shard ledger of lines written but not yet replayed (`lib/steering.py`), and
marks a row COMPLETE only when the turn that consumed it ends. Call and reply
rows, priority rows, other channels and paused shards are never steered; they
wait for their own turn. Rows still unreplayed when the process exits go back to
the queue. Messages arriving together at idle are held for `coalesce_ms` and run
as one batch. `POST /agents/{name}/interrupt` accepts `{"message": ...}` to end
the turn with a control request (the process stays alive) and run the message
next. Per-agent config, all optional:

```yaml
steering: {enabled: true, coalesce_ms: 300, max_lines_per_turn: 8}
```

`enabled: false` restores hold-until-idle exactly.

## Relay (`bin/relay.py`)

The Discord gateway client and the work dispatcher. Two adapters and two
gates:

- **`DiscordAdapter`** — the gateway connection, inbound routing, slash
  commands, attachment download, JSONL capture (a method on this class, not a
  separate adapter).
- **`DispatchAdapter`** — polls `inbox/<agent>/` for work briefs and shells out
  to `bin/invoke-builder.sh` / `bin/invoke-reviewer.sh`. Timeouts: reviewer
  1 hour, builder 6 hours. Concurrency: `MAX_CONCURRENT_BUILDERS=1`,
  `MAX_CONCURRENT_REVIEWERS=2`.
- **`ReplyGate`** — per-channel throttle so agents don't talk over each other.
- **`GuestBudget`** — caps how many turns a *bot* author can consume, 12 by
  default, refilled when a human speaks.

## Scheduler (`bin/scheduler.py`)

Replaces cron, with the container's full environment. The loop ticks every
15 seconds (`SCHEDULER_TICK_SECONDS`) because it also drives the oneshot spool.

| Task | Cadence | Runs |
|---|---|---|
| Heartbeat, primary agent | every 30 min | `bin/heartbeat.sh` → `bin/poke.sh` |
| Heartbeat, relay agent | every 30 min, offset :15 | same |
| Wedge check | every 1 min | `bin/wedge-check.py` |
| Flush deferred messages | every 5 min | `bin/flush-deferred-messages.py` |
| Claude CLI rollback guard | at startup, then hourly | `bin/cli-upgrade-watchdog.sh` |
| Memory consolidation | daily 03:00 | `lib/monitor_jobs/memory_consolidate.py` (also `bin/graph-consolidate.py [--dry-run]`) |
| Health monitor | daily 04:00 | `bin/health-monitor.py` |
| Data purge | daily 04:30 | `bin/purge-data.py` |
| Update check | Mondays 05:00 | `bin/check-updates.sh` — pokes signals on a new release |
| Due one-off work | every tick | `bin/oneshot.py` |

Two of these are deliberately far more frequent than a daily sweep, and it is
worth knowing why.

**Wedge check, every minute.** An agent that is alive but stuck looks healthy
from outside: the process is up, the container is up, and the person waiting
gets nothing. The agent server writes a liveness beacon per agent on every
state change and, throttled to 1/second, on every stream event.
`bin/wedge-check.py` alerts when an agent claims `PROCESSING` while silent for
more than 120 seconds. It alerts through `bin/discord-notify.sh` — the bot
token directly — never through `bin/poke.sh`, because `poke.sh` queues a
message *for an agent*, and the failure being reported is that agents cannot
answer.

**CLI rollback guard, hourly and at startup.** The agent loop runs on the
Claude CLI, which this project does not release and which is replaced under a
running install on every image pull. A bad release installs cleanly, answers
`claude --version`, keeps every health signal green, and simply stops
answering messages. The watchdog notices the installed version has moved away
from the last one this install completed a turn on, runs one probe turn over
the same stream-json wire the agent server uses, and reinstalls the known-good
version if that turn fails. It spends an API call only when the version
actually changed. `bin/upgrade-claude-cli.sh --to 1.2.3` is the same guarantee
for a deliberate upgrade. Both take `--selftest`, which proves the rollback
fires against fake `npm`/`claude` binaries without touching the real install.

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

Each item is one JSON file in `data/oneshot-spool/` holding the **absolute**
epoch second it is due, never the relative span the caller typed. `data/` is a
volume, so the spool outlives the container that wrote it, and the scheduler
replays it at startup before entering its loop.

There is no systemd in the image, so there are no transient timers to re-arm:
being in the spool *is* being armed. A deadline that passed while the
container was down fires immediately, unless it is more than
`ONESHOT_STALE_AFTER_SECONDS` late (default 24h), in which case it is dropped
with a log line rather than arriving days after it was useful.

## MCP tool servers

`.mcp.json` registers two stdio JSON-RPC servers, started by the Claude CLI as
its own children:

- **`system-tools`** → `mcp/tools-server.py`
- **`karakos-admin`** → `mcp/admin-server.py`

### Tools

| Tool | Does |
|---|---|
| `workspace` | System config, agent registry |
| `session` | Finalize / load session summaries |
| `memory`, `graph` | Write and recall durable graph memory; link entities |
| `schedule` | Schedule, list, cancel future work (see `bin/oneshot.py`) |
| `discord` | Read-only Discord access — channels, history |
| `taskboard` | Task tracking, in `data/taskboard.json` |
| `vault` | Git-backed knowledge store |
| `ask_user` | Put a multiple-choice question to a human and block on the answer |

### Asking the user a question (`bin/ask_handler.py`)

Claude Code's built-in `AskUserQuestion` tool does not exist over this
transport: agents run as `claude -p --input-format stream-json`, and in that
mode the CLI leaves the tool out of the session's tool list entirely, even
with `--allowedTools AskUserQuestion`. There is nothing to intercept in the
output stream, so the bridge is a replacement tool rather than an adapter.

```
agent → ask_user (MCP)  ──POST /ask──▶  agent server ──▶ Discord embed + buttons
              ▲                              ▲                      │
              │                              │                   click
        poll GET /ask/{id}          POST /ask/{id}/answer ◀── relay (gateway)
```

- `bin/ask_handler.py` owns the payload shape and the registry state machine.
  It does no I/O, so all three processes can share it.
- The question is posted under the **relay's** bot token: a component
  interaction is delivered only to the application that sent the message, and
  the relay holds the one gateway connection.
- Only the people whose messages started the turn, plus the owner, can answer.
- While a question is outstanding the agent's beacon reads `AWAITING_USER`,
  which `bin/wedge-check.py` does not treat as an active turn — a person taking
  four minutes to decide is not a wedged agent. It flips back to `PROCESSING`
  the moment the question resolves or expires.

### Skill discovery

`mcp/tools-server.py` scans `skills/*/tools.json` at startup; each skill
supplies tool definitions and scripts, dispatched by subprocess with a
`TOOL_ARGS` environment variable. This is **Karakos's own convention**, not
Claude Code's `SKILL.md` frontmatter feature — a frontmatter-only file under
`skills/` will not load. See [EXTENDING.md](EXTENDING.md).

### Audit trail

Every tool call is logged to `data/mcp-tools-audit.db` with timestamp, tool
name, duration and outcome.

## Dashboard

The dashboard is a Next.js app whose source is `dashboard/` in this repository
(see its [README](../dashboard/README.md)), served on `${DASHBOARD_PORT:-3000}`.
The contract between it and the agent server is
[package-backend-contract.md](package-backend-contract.md): the routes the
dashboard may call and the files it reads. `tests/test_agent_server_routes.py`
checks every agent-server path the dashboard calls against the routes
`bin/agent-server.py` registers. The build is in
[EXTENDING.md](EXTENDING.md#dashboard-build).

**Authentication** is a login form (`DASHBOARD_USER` / `DASHBOARD_PASSWORD`)
that sets the `karakos_session` cookie, an HMAC token keyed by `SESSION_SECRET`,
verified by every dashboard API route. Cookie and lifetime settings changed in
2.0 ([UPGRADING.md](UPGRADING.md#auth-and-env-changes)).

**`channel_id "0"` means headless: do not post to Discord.** Every outbound
Discord path short-circuits on it — the reply, the typing indicator, tool
activity lines, crash notices — and `ask_user` refuses to run. It is also the
default when `/message` omits a channel id, which is what `bin/poke.sh
--silent` and `bin/kara` rely on, and what dashboard chat uses.

## Agent lifecycle

**Start.** The server initialises the database, sets every agent `IDLE`, runs
crash recovery, then spawns one subprocess per agent. Each gets:
`agents/<agent>/SYSTEM_PROMPT.md`, every file in `agents/<agent>/persona/`
concatenated, and — if the persona directory is empty —
`agents/<agent>/onboarding.md`, so a brand-new agent interviews you instead of
starting blank. A fresh session also gets the shard's handoff note
(`data/handoff/<shard>.md`) if one was written for it, once, at the top of the
appended prompt.

**Stopping a turn, and starting over.** These four are different and are
routinely confused:

| Action | Subprocess | Session id | Context |
|---|---|---|---|
| `reset` | killed, respawned | **new** | destroyed |
| `reload` | killed, respawned | same | preserved |
| `interrupt` | killed, respawned | same | preserved, partial reply discarded |
| `kill` | killed, stays down | same | preserved |

`interrupt` exists because stream-json has no "stop" message: the only way to
end an in-flight turn is to kill the process to force EOF. The agent is
flagged so the partial text is thrown away rather than posted.

**Context budget, handoff note, and shutdown.** As it reads each agent's
stream, `read_agent_response` tees every raw stream-json line to
`logs/agent-streams/{agent}_<timestamp>.jsonl` — unbuffered, so a separate
process can tail it while the agent still holds the handle, and fail-safe, so
a write error can never break a turn. The file rolls per boot, per day, and at
16 MB; `bin/purge-data.py` drops them after `STREAM_LOG_RETENTION_DAYS`
(default 7).

A long-lived shard re-reads its whole context on every turn, so each agent can
set `context_budget_tokens` (at least 20000) in `agents.yaml`. When a shard's
`sessions.context_tokens` reaches it, the shard is reset at the end of that
turn: the server inserts one internal turn (channel `handoff`, priority 90,
never posted) asking the session that holds the context to write
`data/handoff/<shard>.md`, then restarts the shard on a fresh session and puts
the note, once, at the top of its `--append-system-prompt`. A failed or slow
handoff (`HANDOFF_TURN_TIMEOUT_S`) never blocks the reset; a held shard, an
open account breaker, or a context-overflow error reset without one. Rotated
notes stay in `data/handoff/` (five kept). `handoff_on_reset` defaults to true
for `primary` and `custom` agents and false for the rest; set it false to start
every reset cold. A reload keeps the session, so it leaves the note on disk.
The MCP `session` tool's `finalize` action asks for the same reset at the end
of the current turn. `POST /agents/{name}/reset` stays a cold start;
`?handoff=1` runs the handoff first.

`SIGTERM` triggers `graceful_shutdown()`, which waits for in-flight turns and
stops the subprocesses. It starts no summarizer and no handoff turn; sessions
persist and the next boot resumes them. `bin/summarize-session.py` remains as a
manual tool for one release.

## Agents and the registry

`config/agents.yaml` (parsed only by `lib/registry.py`, schema version 2) is the
one description of the fleet. Each agent has an id (`^[a-z][a-z0-9-]{0,31}$`, the
key of everything it owns), a display `name`, and a `role`: `primary`, `monitor`,
`builder`, `reviewer` or `custom`. Exactly one primary and one monitor are
required. Keys, all optional: `model`, `effort` (`low`, `medium`, `high`,
`xhigh`, `max`), `max_turns`, `timeout`, `token_budget_4h`,
`token_budget_min_pause_s`, `context_budget_tokens`, `handoff_on_reset`,
`reset_mode`, `prompt` (and the legacy `system_prompt`), `tool_streaming`,
`stream_to_channel`, `dashboard_chat`, `allowed_tools`, `disallowed_tools`,
`env`, `label`, `work_stealing`, `steering`, `shards` (each with its `channels`)
and `discord` (`token_env`, `bot_id_env`). Unknown keys warn and are ignored.
The server reads it at start and on reload; the relay re-reads it when the file
changes.

## The turn loop

`lib/turn_loop.py` is the loop the agent server runs per shard: **claim** a batch
of queued rows, **run** one turn on the shard's subprocess, **finish** it (post
the reply, mark rows, re-check the queue). Everything in it takes a shard id.
Extension points are four hooks, `before_claim`, `on_turn_start`, `on_event` and
`on_turn_end`. `before_claim` is where gates live: the operator pause, then the
account breaker, the token budget and the weekly governor (see
[Guard rails](#guard-rails)). A gate that defers a drain changes nothing in the
queue or in shard states and arms a timer to resume.

## Queue and sessions

`data/memory/agent-server.db` holds `message_queue`, `sessions`, `cost_events`
and `rate_limit_state`. The queue's `agent` column holds the **shard id**, and so
does every other key in the server. **The one rule: every piece of
per-conversation runtime state is keyed by shard id.** The only exception is
account-level state, which is a fact about the account (`rate_limit_state` is one
row per window type). The database is in rollback-journal mode, so code reads it
in one hop and never leaves a cursor open across an `await`
([EXTENDING.md](EXTENDING.md#what-survives-an-upgrade)).

## Shards

A shard is one `claude` subprocess of an agent. An agent without `shards:` has a
single shard whose id is the agent id, which is why 1.x history carries over. The
relay routes a Discord message to a shard by the channel ownership in the
registry (`lib/routing.py`); `lib/shards.py` plans the shard set and diffs it on
reload. Adding shards and measuring their memory:
[EXTENDING.md](EXTENDING.md#shards).

## The hive

Shards talk to each other with **buzz** (fire and forget) and **hive call** (a
blocking question), implemented in `lib/hive.py` with routes under `/hive/`:
calls nest at most two deep, a shard cannot call itself or one already waiting on
it, an unanswered call expires with an `expired` reply, and a queued call whose
callee crashes is answered with an error. **Work stealing** (`lib/stealing.py`, off
by default) lets an idle shard take rows waiting behind a busy sibling.
**Steering** (`lib/steering.py`, on by default) writes a message that arrives
mid-turn straight to the running turn. `GET /hive/calls` is the **call log**
([hive-call-log.md](hive-call-log.md)).

## Prompt composition

`lib/prompt_compose.py` builds each shard's system prompt from the shared core
(`agents/CORE.md`), the agent section, optional per-shard text and the fleet
house style (`agents/HOUSE_STYLE.md`). A `<!-- core:insert -->` line is the
**splice marker** for where the core goes; generated blocks are fenced with
`<!-- begin:core -->` and `<!-- end:core -->` **wrapper markers** (and
`house-style`) and are stripped before composing again, so composition is
idempotent. See [EXTENDING.md](EXTENDING.md#prompts).

## Guard rails

- **Rate-limit breaker** (`lib/rate_limits.py`): the CLI reports headroom in-band;
  the state is keyed by **window type** (`five_hour`, `seven_day`, ...), never by
  agent. An open breaker defers every row, human included.
- **Token budget** (`lib/token_budget.py`): per agent, input plus output tokens
  over a trailing four hours across all its shards; a pause lasts at least
  `token_budget_min_pause_s`.
- **Usage governor** (`lib/usage_gate.py`, `config/governor.yaml`): machine-started
  work (heartbeats, scheduled pokes, queued builds) yields when the account's
  seven-day usage reaches a threshold; human messages never do; a `monitor`
  agent's rows are never gated.
- **Cost caps**: `COST_DAILY_LIMIT` and `COST_MONTHLY_LIMIT`, enforced when a
  message is queued.
- **Context handoff**: see [Agent lifecycle](#agent-lifecycle).
- **Bash rails and secrets check**: `system/hooks/bash-safety-rails.py` denies
  destructive commands; `system/check-secrets.py` is a pre-commit check;
  `lib/redact.py` masks credential-shaped strings in the stream-log tee,
  `turn_events` and tool lines.

## Memory

Durable memory is one knowledge graph: `data/memory/graph.db` (SQLite, FTS5,
float32 embeddings), shared by every agent and shard (it is not partitioned).
Code lives in `lib/graph`; the `memory` and `graph` MCP tools read and write it.
Only the migrator (`lib/migrate`) ever opens a 1.x `memory.db`, and it renames
that file `memory.db.migrated` after importing it.

| Table | Holds |
|---|---|
| `entities` | `name`, `kind`, `summary`, `importance`, `embedding`; `entity_aliases` for alternate names |
| `edges` | typed, weighted links between entities (`relation`, `weight`) |
| `observations` | the memories: `kind` (`fact`, `episode`, `pattern`), `content`, `entity_id`, `importance`, `agent`, `domain`, `embedding`, `source` |
| `observation_mentions` | which entities an observation mentions |
| `observations_fts`, `entities_fts` | FTS5 indexes for keyword matching |

**Recall signals.** `recall` scores each candidate on four signals and blends
them: `vec` (embedding cosine, `BAAI/bge-small-en-v1.5` via `fastembed`), `kw`
(FTS5 BM25, with a substring floor), `name` (the query names an entity or
alias) and `imp` (importance ÷ 10). Default weights are `vec 0.50, kw 0.15,
name 0.15, imp 0.20` (`KARAKOS_RECALL_WEIGHTS` overrides). When a signal is
unavailable its weight is dropped and the rest are renormalised.

**Fallback.** Recall degrades rather than failing. If `fastembed` is absent,
the model will not load, or nothing is embedded yet, it runs on `kw`, `name`
and `imp` and says so (`mode: "keyword"` plus a reason).
`KARAKOS_SEMANTIC_RECALL=0` forces that path. The database is queried before
the model, so an install with nothing embedded never loads it.

**Two read paths, one per-prompt injection.**

- The `memory` tool runs in the long-lived tool-server process, so the model
  (about 6 s and 230 MB to load) is paid once and recall is hybrid.
- `system/hooks/inject-recall.py` is the only place a recall block enters a
  prompt. It is a fresh process per prompt, so it runs recall in **fast
  mode**: no model load, signals `kw`, `name`, `imp` only.
  `KARAKOS_RECALL_HOOK_MODE=full` tries the model within a 6 s budget and falls
  back to fast. Source order: an operator override
  (`KARAKOS_RECALL_SOURCE` or `config/recall-source`) **replaces** the graph;
  otherwise the graph. A missing, broken or slow graph yields no block, never
  an error. Automated prompts (the `[KARAKOS_AUTOMATED]` sentinel) are skipped.
- Because automated turns (heartbeats, pokes, scheduled jobs) skip that hook,
  `bin/agent-server.py` also loads the top 50 `fact` observations by
  importance, read through `lib/graph`, into `--append-system-prompt` at spawn
  and resume under its own header (`# Stored Facts (knowledge graph)`). An
  empty or missing graph spawns with no block.

**Writing memory:** `memory.remember` and the `graph` tool write observations,
entities and edges through `GraphStore`. The nightly `memory-consolidate`
job (`lib/graph/consolidate.py`) builds episodes from the previous day's
messages, decays and archives them, merges duplicates, tidies entities and
backfills embeddings.

**Cleaning memory:** agents delete softly. `memory.forget` archives one
observation and `memory.supersede` replaces it with new text (the old row gets
`superseded_by`); both drop out of recall. `graph.delete_entity` archives an
entity and removes its edges, `graph.delete_edge` removes one relation, and
`graph.merge_entities` folds one entity into another (observations, mentions,
aliases and edges move; the old name keeps resolving). Every call writes a
`memory_audit` row with the agent, reason and, for edges, the removed row.
The table is created on first use, so the schema version does not change.
Hard delete is not an agent action. `taskboard.delete` tombstones a task with
`deleted_at` in `data/taskboard.json`; `list` hides it.

## Comms

- **Relay routing.** The relay holds the one Discord websocket and sends each
  message to a shard (`lib/routing.py`); the rules are in
  [EXTENDING.md](EXTENDING.md#routing).
- **Reply gate.** A channel's `reply_gate` decides whether an agent answers a
  message not addressed to it: a heuristic tier, and an optional small-model
  classifier tier that fails closed (`lib/reply_gate_config.py`,
  `lib/reply_classifier.py`). `lib/post_guard.py` keeps empty text and the
  `PASS` convention reply out of channels.
- **Outbox.** Replies are written to `data/outbox/outbox.db` before they are
  sent (`lib/outbox.py`). A row is `pending`, `sending`, `delivered`, `dead` or
  `discarded`; failures retry with exponential backoff (5 s times three per
  attempt, capped at an hour; 429s honour the limit), and a row goes `dead` after
  `DISCORD_OUTBOX_MAX_ATTEMPTS` tries (12) or `DISCORD_OUTBOX_MAX_AGE_S` (86400 s)
  or on a permanent error. Delivery is **at-least-once**, narrowed by a per-chunk
  nonce. Operators use `python3 lib/outbox.py {stats,list,show,retry,discard}`
  with the server down or `GET /outbox`, `GET /outbox/{id}`, `POST
  /outbox/{id}/retry` and `POST /outbox/{id}/discard` with it up. `GET /health`
  carries `outbox` (`pending`, `sending`, `dead`, `oldest_pending_age_s`);
  `dead_letters` counts dead rows and `dead_letter_path` is deprecated.
- **Discord switches** (all off by default; `threads`, `reaction_notices`,
  `edit_reroute`, `suppress_embeds`; `tool_streaming` is a per-channel override of
  the agent's setting): [DISCORD_SETUP.md](DISCORD_SETUP.md#optional-behaviours).
- **Pause and effort.** `/pause [minutes]` and `/resume` hold and release a
  shard's queue (`lib/operator_pause.py`, `data/operator-pause.json`; the turn in
  progress finishes). `/effort <level>` sets an agent-level `--effort` override
  (`lib/runtime_overrides.py`, `data/runtime-overrides.json`); the registry file
  is never edited at runtime. Routes: `POST /agents/{name}/pause`, `/resume`,
  `/effort`.

## Monitoring

The `monitor` agent is a cheap model with a tool deny list (no shell, write or web
tools; `MONITOR_DISALLOWED_TOOLS` in `lib/registry.py`). Around it, plain code
does the detecting: scheduled jobs touch **heartbeats** (`lib/heartbeats.py`),
**drift** compares the job table with the live schedule (`lib/drift.py`),
**stall diagnosis** says why a shard is silent (`lib/stall.py`), and
`lib/monitor_tick.py` runs once a minute and turns findings into alerts.
**Alerts** (`lib/alerts.py`, `config/monitor.yaml`) post straight to Discord
through `bin/discord-notify.sh`, never through the agent poke path, because a
wedged agent never reads its queue. `lib/outbox_audit.py` audits message
delivery from the outbox.

## Build queue

Off by default (`config/build-queue.yaml`). When enabled, briefs in
`inbox/builder/` and `inbox/reviewer/` become rows in `data/build-queue.db`
(`lib/buildq.py`, operator CLI `bin/buildq`); `lib/build_dispatcher.py` admits
them against per-host concurrency and a resource probe (`lib/build_hosts.py`),
runs them locally or over ssh on a remote host (`lib/build_run.py`,
`bin/build-runner.sh`), and fails a build that opened no PR. Provisioning a remote
host: [EXTENDING.md](EXTENDING.md#build-queue).

## Credentials

What each process holds, and what it hands on:

| Process | Holds | Passes on |
|---|---|---|
| `agent-server` | every variable in `config/.env`, the mounted `~/.claude` login | to each agent subprocess: the allowlist, the agent's `env:`, and the identity variables below |
| `relay` | the Discord bot tokens | nothing to agents |
| `claude` subprocess (and its hooks and shell commands) | the allowlist (`PATH`, `HOME`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, ...), the variables its `env:` names, and `KARAKOS_AGENT`, `KARAKOS_SHARD`, `WORKSPACE_ROOT`, `AGENT_SERVER_PORT`, `AGENT_SERVER_URL` and `AGENT_SERVER_TOKEN` | to its MCP servers, unchanged |
| MCP tool servers | what the subprocess passed | calls back into the agent server with the token |
| dashboard | `SESSION_SECRET`, its login, `AGENT_SERVER_TOKEN` | proxies authenticated calls |

**`AGENT_SERVER_TOKEN` is still passed to every agent subprocess. This is a
residual.** The agent's MCP servers call back into the server with it, so any
tool the agent runs can use it against every authenticated route (`/message`,
`/agents/...`, `/outbox/...`, `/hive/...`, the graph routes). What limits it: the
server is published on `127.0.0.1` only (`config/docker-compose.yml`), the
monitor agent's deny list removes its shell, write and web tools, and the
allowlist keeps every other secret out. A scoped per-shard credential, after
which the token would be dropped from the subprocess environment, is a post-2.0
item.

## Protected paths

`config/protected-paths.json`, enforced by a pre-commit hook that
`bin/entrypoint.sh` installs at every start.

**Tier 1 — hard block.** A builder agent cannot commit these at all:

```
system/   config/   .karakos/   Dockerfile
bin/agent-server.py   bin/relay.py   bin/entrypoint.sh   bin/scheduler.py
config/protected-paths.json
```

**Tier 2 — review required:** `bin/`, `agents/templates/`,
`mcp/tools-server.py`.

**Overrides — always writable**, even though they sit under a protected
prefix: `agents/*/persona/`, `agents/*/journal/`, `agents/*/inbox/`. That
carve-out is what lets an agent maintain its own persona and journal without
being handed the keys to its own process lifecycle.

## Data layout

```
data/                                  # named volume
├── .schema-version                    # the stamp: schema, package, migrated_from
├── memory/
│   ├── agent-server.db                # message_queue, sessions, cost_events,
│   │                                  #   rate_limit_state
│   └── graph.db                       # knowledge graph: entities, edges,
│                                      #   observations, embeddings
├── mcp-tools-audit.db                 # tool_calls
├── messages/
│   └── messages-YYYY-MM-DD.jsonl      # daily capture
├── attachments/<discord_message_id>/  # downloaded files
├── deferred-messages/                 # spooled while the server was down
│   ├── stale/                         # too old to re-fire
│   └── invalid/                       # unparseable
├── oneshot-spool/*.oneshot.json       # agent-scheduled future work
├── health/
│   ├── agents/<agent>.json            # liveness beacons
│   ├── relay.json  scheduler.json
│   ├── mcp-tools.json  memory-consolidate.json
│   ├── claude-cli.json                # known-good CLI version
│   └── wedge-check-state.json
├── taskboard.json
├── outbox/outbox.db                   # durable Discord replies: retry + audit (6.1)
├── build-queue.db                     # build queue (when enabled)
├── discord-dead-letter.jsonl.migrated # 1.x dead letters, imported into the outbox and kept
├── operator-pause.json                # /pause holds, keyed by shard id
├── runtime-overrides.json             # /effort overrides, keyed by agent id
├── migration-reports/                 # migration-report.md from the migrator
├── handoff/<shard>.md                 # note for the next fresh session (rotated)
└── stop-hook-extensions.json

logs/                                  # named volume
├── agent-server.log  relay.log  scheduler.log  supervisord.log
├── health-alerts.log
├── blocked-bash.jsonl                 # commands the bash safety hooks denied
├── summarizer-audit.jsonl  git-events.jsonl  hook-events.log
├── agent-streams/<agent>_<ts>.jsonl   # raw stream-json, fed to the summarizer
└── session-summaries/<agent>-<ts>.md

inbox/<agent>/                         # named volume — builder/reviewer briefs

backups/                               # in the checkout: migrator backups,
                                       #   pre-2.0-<timestamp>/ with MANIFEST.json
```

Note `data/memory/agent-server.db` — the queue database lives under
`memory/`, not at the top of `data/`.

## The upgrade model

A data directory carries a stamp, `data/.schema-version`. The 2.0 image checks it
before touching anything and **exits 78** on an unstamped non-empty directory or
an older schema. A fresh, empty data directory is stamped by setup. The migrator
(`python3 -m lib.migrate`, host wrapper `bin/karakos migrate`) is the only writer
of existing data: it detects the layout, takes a backup, runs the steps in
`lib/migrate/steps/`, verifies each and writes the stamp last. Backups are the
only way back; memory has no downgrade. Operator procedure:
[UPGRADING.md](UPGRADING.md). Rule for contributors: runtime state is keyed by
shard id, and only `lib/migrate/steps/` mutates existing data.

## Known gaps

Documented so you don't spend an evening deciding whether it's your install.
Each is a real limit of the code, not a configuration mistake.

- **Session-summary retention mis-buckets hyphenated agent names.**
  `bin/purge-data.py` splits the agent out of the filename at the first
  hyphen, so `test-agent` and `test-bot` share one 30-file budget and evict
  each other. ([#156](https://github.com/mcarmody/karakos-package/issues/156))
- **Context management is a handoff reset, not compaction.** `context_budget_tokens`
  resets a shard at the end of the turn that reaches it, after a handoff note
  (see [Agent lifecycle](#agent-lifecycle)). `reset_mode: compact` is inert: it
  behaves as `reset` until `/compact` over stream-json is proven against the real
  CLI (`COMPACT_VERIFIED` in `lib/session_policy.py`). The `compaction_count` and
  `last_compacted` columns of `sessions` are not read by anything.
- **`AGENT_SERVER_TOKEN` reaches agent subprocesses** (see [Credentials](#credentials)).
  A scoped credential is post-2.0.
- **No host-side watchdog for a dead scheduler.** The container health check and
  supervisord's restart are what exist.
- **The migrator fills `env:` only from `${NAME}` references in `.mcp.json`**
  (not from skill or hook scripts), and `logs/` and `inbox/` are not in the
  backup ([UPGRADING.md](UPGRADING.md#the-real-run)). The 2.0 compose template
  keeps the `.karakos` mount because the owner and system name are still read
  from `.karakos/config.json`.

## Where each 2.0 feature lives

One row per step; every path exists in the checkout (`tests/test_docs.py` checks).

| Step | Feature | Files |
|---|---|---|
| 0.5 | Coupling check, review gates | `system/check-coupling.sh`, `system/coupling-denylist.txt` |
| 1.0 | Schema stamp and migrator core | `lib/migrate/guard.py`, `lib/migrate/runner.py`, `lib/migrate/backup.py`, `lib/migrate/detect.py` |
| 1.1a / 1.1b | Registry and its migration | `lib/registry.py`, `lib/migrate/steps/10_registry.py` |
| 1.2 | Queue schema | `lib/msgqueue.py`, `lib/migrate/steps/20_queue.py` |
| 1.3 / 1.3b | Prompt composition, legacy prompt flags | `lib/prompt_compose.py`, `agents/CORE.md`, `agents/HOUSE_STYLE.md` |
| 1.4 | Safety hooks | `bin/hooks-sync.py`, `config/hooks.json`, `system/hooks/bash-safety-rails.py`, `system/check-secrets.py` |
| 1.5 | Context metric | `lib/session_policy.py`, `lib/migrate/steps/30_sessions.py` |
| 1.6 | Subprocess env allowlist | `lib/spawn_env.py` |
| 1.7 | Small portable fixes | `system/check-coupling.sh` |
| 2.0 | Turn loop | `lib/turn_loop.py` |
| 2.1 | Shards | `lib/shards.py`, `lib/registry.py` |
| 2.2 | Relay routing | `lib/routing.py`, `bin/relay.py` |
| 2.3 | Buzz and hive call | `lib/hive.py`, `docs/hive-call-log.md` |
| 2.4 | Work stealing | `lib/stealing.py` |
| 2.5 | Steering | `lib/steering.py` |
| 2.6 | Context handoff | `lib/session_policy.py`, `bin/agent-server.py` |
| 2.7 | Rate-limit breaker, budget, governor | `lib/rate_limits.py`, `lib/token_budget.py`, `lib/usage_gate.py`, `lib/usage_governor.py`, `config/governor.yaml`, `lib/migrate/steps/35_rate_limit.py` |
| 3.1 | Primary template | `agents/templates/primary.md`, `bin/create-agent.sh` |
| 3.2 | Monitor agent | `agents/templates/monitor.md`, `lib/monitor_tick.py`, `lib/drift.py`, `lib/stall.py`, `lib/alerts.py`, `lib/heartbeats.py`, `lib/outbox_audit.py`, `lib/migrate/steps/12_monitor.py` |
| 3.3 | Build queue | `lib/buildq.py`, `lib/build_dispatcher.py`, `lib/build_hosts.py`, `lib/build_run.py`, `bin/buildq`, `bin/build-runner.sh`, `lib/migrate/steps/50_build_queue.py` |
| 4.1 | Graph store | `lib/graph/store.py`, `lib/graph/schema.py`, `lib/graph/embed.py` |
| 4.2 / 4.2b | Memory tools, rewired consumers | `lib/graph/tools.py`, `lib/graph/recall.py`, `mcp/tools-server.py`, `system/hooks/inject-recall.py` |
| 4.3 | Consolidation job | `lib/graph/consolidate.py`, `lib/monitor_jobs/memory_consolidate.py`, `bin/graph-consolidate.py` |
| 4.4 | Memory migrator | `lib/migrate/steps/40_memory.py` |
| 5.0 / 5.4 | Dashboard adapter and fleet pages (`dashboard/`) | `docs/package-backend-contract.md` |
| 5.1 / 5.2 | Route inventory, auth and env (`dashboard/`) | `docs/package-backend-contract.md` |
| 5.3b | Dashboard source in this repo, built in the image | `dashboard/`, `Dockerfile` |
| 5.5 | Memory browser read API | `lib/graph/browse.py`, `docs/graph-browse-api.md` |
| 6.1 | Discord outbox | `lib/outbox.py`, `lib/migrate/steps/60_outbox.py` |
| 6.2 | Discord UX switches | `lib/discord_ux.py` |
| 6.3 | Reply gate classifier | `lib/reply_gate_config.py`, `lib/reply_classifier.py`, `lib/post_guard.py` |
| 6.4 | Pause, effort, redaction | `lib/operator_pause.py`, `lib/runtime_overrides.py`, `lib/redact.py` |
| 7.1 | `karakos migrate` | `bin/karakos`, `bin/karakos-migrate`, `lib/migrate/__main__.py`, `lib/migrate/compose.py` |
