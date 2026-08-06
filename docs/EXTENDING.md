# Extending Karakos

Guide to customizing agents, adding skills, and growing the system.

## Adding a New Agent

```bash
# Inside the container
bin/create-agent.sh --template primary --model sonnet oracle

# With a Discord bot identity
bin/create-agent.sh --template builder --model sonnet \
  --discord-token "$DISCORD_BOT_TOKEN_BUILDER" builder
```

Templates: `primary`, `relay`, `builder`, `reviewer`

The agent is hot-registered — no server restart needed.

## Customizing Agent Personality

Each agent has a persona directory:

```
agents/{name}/
├── SYSTEM_PROMPT.md    # Core instructions (generated from template)
├── persona/
│   └── voice.md        # Voice, tone, behavioral rules
├── inbox/              # Incoming work briefs
└── journal/            # Agent-written logs
```

Edit `persona/voice.md` to customize how the agent communicates. This file is loaded fresh on each session start — no restart needed.

### Example voice.md

```markdown
# Voice

## Tone
Direct and concise. No filler words. Technical when appropriate.

## Addressing Style
Call the owner by first name.

## Boundaries
Never discuss politics or religion. Redirect to practical topics.
```

## Adding a Skill

Skills add new tools to the MCP server. They're automatically discovered at startup.

**This is not Claude Code's built-in Agent Skills feature.** Claude Code has
its own native skill system: a `SKILL.md` file with YAML frontmatter,
auto-discovered from `.claude/skills/`. Karakos "skills" are a separate,
package-specific convention — a `tools.json` schema plus a `scripts/`
implementation, discovered by `mcp/tools-server.py` and exposed as MCP
tools. A frontmatter-only `SKILL.md` dropped under `skills/` will not load
here; it needs `tools.json` and `scripts/` as shown below.

### 1. Create the Skill Directory

```bash
cp -r skills/hello-world skills/my-skill
```

### 2. Define Tools

Edit `skills/my-skill/tools.json`:

```json
{
  "skill_name": "my-skill",
  "version": "1.0.0",
  "description": "What this skill does",
  "tools": [
    {
      "name": "my_tool",
      "description": "What this tool does",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "Search query"
          }
        },
        "required": ["query"]
      }
    }
  ]
}
```

### 3. Implement

Create `skills/my-skill/scripts/my_tool.py`:

```python
#!/usr/bin/env python3
import json, os

args = json.loads(os.environ.get("TOOL_ARGS", "{}"))
query = args.get("query", "")

# Do something useful
result = {"answer": f"You asked about: {query}"}

print(json.dumps(result))
```

Scripts receive `TOOL_ARGS` (JSON) and `WORKSPACE_ROOT` via environment. Print JSON to stdout. Exit code 0 = success.

### 4. Test

```bash
python3 mcp/tools-server.py --test-tool my_tool '{"query": "test"}'
```

### 5. Activate

Reset the agent session (dashboard → Agents → Reset, or via API). The MCP server restarts with the agent and discovers the new skill.

## Using the Builder Agent

The builder agent receives specs as markdown files in its inbox and implements them on feature branches.

### Writing a Spec

Create a file in `agents/builder/inbox/`:

```markdown
---
type: build
target_branch: main
---

# Feature: User Preferences

## Summary
Add a user preferences system that persists settings to a JSON file.

## Requirements
1. Create `data/preferences.json` with default values
2. Add `preferences` tool to MCP server (get/set actions)
3. Primary agent can read and update preferences

## Acceptance Criteria
- [ ] Preferences persist across restarts
- [ ] Default values provided for new installations
```

### Triggering a Build

The dispatch adapter watches inbox directories. When it finds a spec:

1. Invokes `bin/invoke-builder.sh` with the spec path
2. Builder reads the spec, creates a feature branch, implements, and opens a PR
3. Cost is posted to #signals
4. Owner reviews and merges

### Using the Reviewer Agent

Send a spec and codebase for adversarial review:

```bash
bin/invoke-reviewer.sh --spec agents/builder/inbox/my-feature.md \
  --branch builder/my-feature --mode spec
```

The reviewer returns a verdict: APPROVE, REVISE, or REJECT.

## Inter-Agent Communication

Agents communicate via `bin/poke.sh`:

```bash
# Send a message to an agent
bin/poke.sh agent-name channel-name "Your message here"

# Send to the primary agent on #general
bin/poke.sh primary general "Status report please"
```

For file-based dispatch, drop files in `inbox/{agent-name}/`.

## Self-Modification

The system can modify itself through the builder agent:

1. Write a spec describing the change
2. Builder implements on a feature branch
3. Reviewer provides adversarial feedback
4. Owner merges the PR
5. Protected paths (Tier 1) block unauthorized changes to core files

### What Requires Restart

| Changed File | Restart Needed | How |
|-------------|---------------|-----|
| `persona/voice.md` | None | Loaded fresh each session |
| `skills/*/` | Agent session reset | Dashboard → Reset |
| `config/agents.json` | Agent server restart | POST `/restart/server` |
| `bin/agent-server.py` | Agent server restart | POST `/restart/server` |
| `Dockerfile` | Container rebuild | see [Local development build](#local-development-build) |

## Local Development Build

Production installs pull a prebuilt image from GHCR (`docker compose pull`).
If you are modifying the `Dockerfile` or Python/Node dependencies and need to
test those changes before a release, use the dev compose override:

```bash
docker compose \
  -f config/docker-compose.yml \
  -f config/docker-compose.dev.yml \
  up --build -d
```

`config/docker-compose.dev.yml` overlays `build: .` back onto the service so
your local changes are compiled into an image named `karakos-dev:local`.
Bring the stack back down and return to the prebuilt image at any time with:

```bash
docker compose down
docker compose -f config/docker-compose.yml up -d
```

The dev override is intentionally not committed to production flows — it is
only for contributors iterating on the image itself.

## Environment Variables

All configuration lives in `config/.env`. Key variables:

| Variable | Description |
|----------|-------------|
| *(Anthropic auth)* | Handled by `claude login` — no API key in env |
| `AGENT_SERVER_TOKEN` | Bearer token for API auth |
| `COST_DAILY_LIMIT` | Daily spend cap in USD |
| `COST_MONTHLY_LIMIT` | Monthly spend cap in USD |
| `MAX_CONCURRENT_BUILDERS` | Parallel builder agents |
| `MEMORY_DECAY_RATE` | Episode importance decay (0-1) |
| `MESSAGE_RETENTION_DAYS` | JSONL log retention |

## Claude Code Hooks

`config/claude-settings.json` is a package-owned Claude Code settings file
passed via `--settings` on every agent's `claude` spawn line
(`bin/agent-server.py`), rather than a `.claude/` directory an installer
would have to scaffold and a user could delete. It wires hook events and
carries the tool permission policy. Shipped hooks:

| Event | Script | Does |
|---|---|---|
| `UserPromptSubmit` | `system/hooks/log-user-prompt.sh` | Appends one line to `logs/hook-events.log` per prompt — proves the hook pipeline is live. |
| `UserPromptSubmit` | `system/hooks/inject-recall.py` | Re-injects a recall block before every user message (see below). |
| `PreToolUse` (`Edit\|Write\|MultiEdit\|Read`) | `system/hooks/resolve-symlink-edit.py` | Rewrites a path through a symlink to its realpath so the harness's "refusing to write through symlink" rejection never reaches the model. |
| `PreToolUse` (`Bash`) | `system/hooks/rewrite-sleep-poll.py` | Rewrites a leading `sleep N` into `wait-for.sh --sleep N` so the sandbox's blocked-foreground-sleep rejection never reaches the model. |
| `Stop` | `system/hooks/stop-deferred-work.py` | Catches "I'll do that shortly"-style deferrals in the final reply and forces the turn to continue, capped at 2 extensions. |

All hook scripts are fail-safe by construction: a parse error, a missing
field, or an unexpected shape is swallowed and the tool call / turn
proceeds exactly as it would unmodified. None of them ever raise into the
CLI.

### Recall re-injection (`inject-recall.py`)

Without this hook, everything an agent knows enters once, at spawn, via
`--append-system-prompt`, and a long-running session answers every question
from whatever was true when it started. `inject-recall.py` re-reads a
recall source on every `UserPromptSubmit` and folds it into the turn via
Claude Code's `hookSpecificOutput.additionalContext`.

The package ships no memory store of its own — the recall source is a
documented, swappable interface, resolved from `KARAKOS_RECALL_SOURCE`
(default `$WORKSPACE_ROOT/config/recall-source`):

- **Path does not exist** — no-op. Not an error; a fresh install with no
  recall source configured behaves exactly as before this hook existed.
- **Path is executable** — run with the pending user prompt text on stdin;
  its stdout becomes the recall block. A non-zero exit, a crash, or a
  timeout (`KARAKOS_RECALL_TIMEOUT_S`, default 10s) are all treated as "no
  recall available," never as an error that blocks the turn.
- **Path is a plain file** — read verbatim, every turn, as a static recall
  block (e.g. a hand-maintained facts file).

Automated traffic — system pokes, heartbeats, and task-complete
notifications sent through `bin/poke.sh` (always `is_bot=1`) — skips
recall entirely, so scheduled/background turns don't pay for it.
`bin/agent-server.py` stamps a `[KARAKOS_AUTOMATED]` sentinel onto the
front of any message batch where every message is bot-originated; the hook
recognizes that same literal string as its skip gate. A batch with even
one human message alongside automated ones is left unmarked, so a human
reply riding along in the same batch still gets a fresh recall block.

### Tool permissions (`permissions.allow` / `permissions.deny`)

`bin/agent-server.py` passes `--dangerously-skip-permissions` on every
agent spawn, which is the CLI's own bypass-all-approval-prompts mode.
`permissions.allow` / `permissions.deny` in `config/claude-settings.json`
sit above that and are **not** overridden by it — verified against the
real CLI:

- A tool named in `permissions.deny` in full (e.g. `"WebFetch"`) is dropped
  from the session's tool list entirely at spawn. The model cannot call it
  — there is nothing to invoke, and no runtime prompt for
  `--dangerously-skip-permissions` to bypass.
- A fine-grained rule (e.g. `"Bash(curl:*)"`) leaves the tool available but
  declines a matching call at request time. That decline shows up in the
  stream-json `result` event's `permission_denials` list, which
  `read_agent_response()` logs as a warning
  (`<agent> permission denied: tool=... input=...`).
- `read_agent_response()` also logs the resolved tool list from the
  session's opening `system`/`init` event, so a full-tool deny is visible
  in `logs/agent-server.log` even though it never produces a runtime
  denial event.

Both `allow` and `deny` default to `[]` (no-op) and apply to every agent,
since `config/claude-settings.json` is currently one file shared across
all agents — there is no per-agent settings file.

### Per-agent environment

`config/claude-settings.json`'s own `env` key is applied by the CLI to its
own process environment, but since that file is shared, it's install-wide,
not per-agent. For environment scoped to a single agent, add an `env`
object to that agent's entry in `config/agents.json`:

```json
{
  "agents": {
    "researcher": {
      "system_prompt": "agents/researcher/SYSTEM_PROMPT.md",
      "env": { "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5" }
    }
  }
}
```

`bin/agent-server.py` layers this onto its own environment (not a
replacement) when spawning that agent's subprocess, so the agent still
inherits `WORKSPACE_ROOT`, API credentials, etc. An agent with no `env` key
spawns exactly as before this feature existed (`env=None`, plain inherit).
