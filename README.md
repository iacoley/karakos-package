# Karakos

[![CI](https://github.com/mcarmody/karakos-package/actions/workflows/ci.yml/badge.svg)](https://github.com/mcarmody/karakos-package/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker Required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Anthropic Claude](https://img.shields.io/badge/Powered_by-Claude-orange)](https://www.anthropic.com/claude)

**A standing Claude agent that lives on your own hardware, answers in your
Discord, and remembers you between restarts.**

Karakos is one Docker container. Point it at a Discord server and an Anthropic
login, and you get an assistant that is always up: it holds a conversation
across process restarts, keeps a searchable knowledge graph of what it learns, tracks what it
spends and stops at a cap you set, and can be extended with your own tools
without you writing any process-management code.

The infrastructure you would otherwise build first — session lifecycle,
memory consolidation, cost guards, a message queue, a web dashboard, agent
orchestration — is the part this package is.

---

## Contents

- [How it fits together](#how-it-fits-together)
- [Install](#install)
- [Running it day to day](#running-it-day-to-day)
- [Talking to it](#talking-to-it)
- [What ships in the box](#what-ships-in-the-box)
- [Requirements and cost](#requirements-and-cost)
- [Documentation](#documentation)

---

## How it fits together

Everything runs inside **one container**. There is no multi-service compose
stack to reason about — `supervisord` starts four long-lived processes that
share a workspace on disk.

```
        You                          You
     (Discord)                    (browser)
         │                            │
         │  message                   │  https :3000
         ▼                            ▼
  ┌─────────────┐              ┌─────────────┐
  │   relay.py  │              │  dashboard  │
  │  (gateway)  │              │  (Next.js)  │
  └──────┬──────┘              └──────┬──────┘
         │                            │
         │      HTTP + bearer token   │
         └────────────┬───────────────┘
                      ▼
             ┌──────────────────┐        ┌──────────────┐
             │ agent-server.py  │◀──────▶│ scheduler.py │
             │  :18791          │  poke  │ (heartbeats, │
             │                  │        │  oneshots)   │
             │  • message queue │        └──────────────┘
             │  • cost guard    │
             │  • turn loop     │
             └────────┬─────────┘
                      │ stdin/stdout, stream-json
                      ▼
             ┌──────────────────┐        ┌──────────────┐
             │  claude CLI      │───────▶│ MCP tools    │
             │  (one child      │  MCP   │ server       │
             │   per agent)     │        │ + skills/    │
             └────────┬─────────┘        └──────────────┘
                      │
                      ▼
             ┌──────────────────────────────────────────┐
             │  data/  — SQLite + JSONL, on a volume    │
             │  queue · cost · memory · audit · spool   │
             └──────────────────────────────────────────┘
```

The short version: **Discord and the browser are both just clients.** They put
a message on the agent server's queue; the agent server owns the Claude
subprocess, runs one turn at a time per agent, and writes the answer back the
way it came in. Nothing else talks to Claude directly.

Full technical reference: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Install

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.sh | bash
```

**Windows** (PowerShell as administrator)

```powershell
irm https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.ps1 | iex
```

The installer checks prerequisites (Docker, Git, jq), clones the repo to
`~/karakos`, then hands off to the setup wizard, which asks you for a system
name, an Anthropic login, your Discord bot token and channel IDs, and your
spend caps. It finishes by pulling the prebuilt image from GHCR (~1.2 GB, no
local build) and starting the container.

When it's done, open **http://localhost:3000** and log in as `admin` with the
password the wizard printed.

Prefer to drive it yourself, or already have the repo?

```bash
git clone https://github.com/mcarmody/karakos-package.git ~/karakos
cd ~/karakos
./setup.sh          # wizard — resumable, safe to quit and re-run
make install        # preflight, pull, start
```

Step-by-step walkthrough, including the Discord bot: **[docs/QUICKSTART.md](docs/QUICKSTART.md)**
and **[docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md)**.

---

## Running it day to day

The compose file lives at `config/docker-compose.yml`, not at the repo root,
so a bare `docker compose up` in your checkout will not find it. Use the
`make` targets, which point at the right file for you:

| Command | What it does |
|---|---|
| `make install` | Preflight checks, pull the image, start the container |
| `make up` | Start (or restart) the container |
| `make down` | Stop it — agents finalize their sessions first, up to 45s |
| `make logs` | Follow the container log |
| `make shell` | Open a shell inside the container as the `karakos` user |
| `make pull` | Fetch a newer image without starting it |
| `make preflight` | Host checks only, starts nothing |
| `make help` | List the targets |

If you'd rather type Docker directly, every command needs the file flag:

```bash
docker compose -f ~/karakos/config/docker-compose.yml logs -f
docker compose -f ~/karakos/config/docker-compose.yml exec karakos bash
```

Address the container by its **service** name, `karakos`. There is one
service, so `docker compose logs relay` or `docker compose logs dashboard`
will not work — those are processes inside the single container, and their
individual logs are under `logs/` in the workspace.

**Pinning a version.** Set `KARAKOS_VERSION` in `config/.env` to stay on a
release and upgrade on your own schedule; the default is `latest`. See
**[docs/UPGRADING.md](docs/UPGRADING.md)**.

---

## Talking to it

**Discord.** Message the bot in a channel it watches. That is the main path
and the one the system is designed around.

**Dashboard** at `http://localhost:3000`: status, chat, costs, conversations and
the fleet. Its source is [`dashboard/`](dashboard/README.md), built into the
image; the routes it uses are in
[docs/package-backend-contract.md](docs/package-backend-contract.md). Chat
replies stay out of Discord.

**Terminal.** `bin/kara` is a small Python CLI that speaks the same HTTP API
as the dashboard:

```bash
./bin/kara "what's on my calendar?"     # one-shot
echo "summarize this" | ./bin/kara      # from a pipe
./bin/kara                              # interactive REPL
```

REPL commands: `/health`, `/agents`, `/agent <name>`, `/cost`, `/reset`,
`/reload`, `/restart`, `/help`, `/quit`. Set `AGENT_SERVER_TOKEN` (from
`config/.env`); `AGENT_SERVER_URL` defaults to `http://127.0.0.1:18791`.

---

## What ships in the box

**Agents.** Two run by default: **primary**, the general-purpose assistant you
talk to, and the **monitor** (you name it in the wizard), a cheap agent that
watches heartbeats, drift and stalls and raises alerts. Agents can have several
**shards** (parallel conversations that share memory) which talk to each other
through the **hive** (`buzz` and `hive_call`). Two more are one command away — **builder**, which takes a spec and
opens a pull request, and **reviewer**, which reviews adversarially before
merge:

```bash
make shell
bin/create-agent.sh --template builder --model sonnet builder
```

On first boot a fresh agent runs an onboarding conversation to learn who you
are, rather than starting from a blank persona.

**Memory.** Durable memory is a knowledge graph (entities, links and
observations) in `data/memory/graph.db`. Each human prompt gets a short recall
block from it, and top facts load at session start; semantic search runs on
local embeddings, so recall costs no API calls.

**Delivery.** Discord replies go through a durable **outbox** with retries and an
audit trail, so a Discord outage delays an answer instead of losing it.

**Cost control.** Every turn's spend is recorded per agent. Daily and monthly
caps are enforced at the point a message is queued, and warnings post to your
signals channel before the cap bites.

**Scheduled work.** Heartbeats, health sweeps, memory consolidation and data
purges run on a built-in scheduler. Agents can also schedule arbitrary
one-off work at runtime — "check back in ten minutes" is a real mechanism,
and it survives a container restart.

**Self-modification, fenced.** Builder agents can change the system's own
code, but `config/protected-paths.json` hard-blocks the files that own
process lifecycle and security, and flags a second tier for review.

**Your own tools.** Drop a `tools.json` and a script into `skills/<name>/` and
the MCP server registers it at startup. See
**[docs/EXTENDING.md](docs/EXTENDING.md)** — and note that this is Karakos's
own skill convention, not Claude Code's `SKILL.md` frontmatter feature.

---

## Requirements and cost

| | |
|---|---|
| **Hardware** | 4 GB RAM minimum, 8 GB recommended; 2+ cores; 10 GB disk |
| **OS** | Ubuntu 22.04+, Debian 12+, macOS 12+, Windows 10/11 |
| **Software** | Docker Engine 24+ with Compose v2, Git, `jq` |
| **Auth** | An Anthropic account — `claude login`, no API key to paste |
| **Network** | A stable connection; the box is expected to run 24/7 |

**Expected spend: $5–15/week** for typical household use, and the cost caps
are there so a surprise stays small.

A Raspberry Pi 4 or 5, a mini PC, or a small VM all work.

---

## Documentation

| Doc | Read it when |
|---|---|
| [QUICKSTART.md](docs/QUICKSTART.md) | Installing for the first time |
| [DISCORD_SETUP.md](docs/DISCORD_SETUP.md) | Creating and inviting the bot |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | You want to know how it actually works |
| [EXTENDING.md](docs/EXTENDING.md) | Adding skills, tools or agents |
| [UPGRADING.md](docs/UPGRADING.md) | Moving to a new release, or from 1.x to 2.0 |
| [OVERLAY.md](docs/OVERLAY.md) | Keeping your config, personas and skills in a private overlay repo instead of a fork |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Sending a patch |
| Agents: [AGENTS.md](AGENTS.md), [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) | A coding agent installing, repairing, improving or contributing to Karakos |

---

## License

MIT. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

Built with [Claude Code](https://claude.ai/claude-code) (Anthropic).
