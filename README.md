# Karakos

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker Required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Anthropic Claude](https://img.shields.io/badge/Powered_by-Claude-orange)](https://www.anthropic.com/claude)

A self-contained, installable multi-agent household assistant system powered by Claude.

## Install

**Linux / macOS** — one command:
```bash
curl -fsSL https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.sh | bash
```

**Windows** — one command (PowerShell as admin):
```powershell
irm https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.ps1 | iex
```

The installer handles prerequisites (Docker, Git, jq), clones the repo, runs the setup wizard, pulls the prebuilt image from GHCR, and starts the system. Open http://localhost:3000 when it's done.

## What is Karakos?

Karakos is a multi-agent system that provides:
- **Discord integration** — Agents respond in your Discord server
- **Local dashboard** — Web interface for chat and monitoring
- **Memory system** — Episodic memory with consolidation and recall
- **Coding stack** — Builder and reviewer agents that can modify the system
- **Session persistence** — Context preserved across restarts
- **Cost tracking** — Monitor API spend with configurable limits

## System Requirements

- **Hardware**: 4GB RAM minimum (8GB recommended), 2+ CPU cores, 10GB disk space
- **OS**: Windows 10/11, Ubuntu 22.04+, Debian 12+, macOS 12+
- **Software**: Docker Engine 24+ with Compose v2
- **Network**: Stable internet for Anthropic API calls
- **Runtime**: 24/7 recommended

**Expected cost**: $5-15/week typical usage

## Installation

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for detailed installation instructions.

## CLI access

`bin/kara` is a Python CLI that talks to the agent-server's `/message`
endpoint and tails the response from `message_queue` — same transport as
the dashboard chat.

```bash
# one-shot
./bin/kara "what's on my calendar?"
echo "summarize this" | ./bin/kara

# REPL (interactive, slash commands)
./bin/kara
```

Slash commands inside the REPL: `/health`, `/agents`, `/agent <name>`,
`/cost`, `/reset`, `/reload`, `/restart` (macOS), `/help`, `/quit`.

Env vars: `AGENT_SERVER_TOKEN` (required), `AGENT_SERVER_URL`
(default `http://127.0.0.1:18791`), `KARA_AGENT`, `KARA_CHANNEL`
(default `cli`), `KARA_TIMEOUT` (default `300`s).

## Documentation

- [QUICKSTART.md](docs/QUICKSTART.md) — Installation and first steps
- [DISCORD_SETUP.md](docs/DISCORD_SETUP.md) — Discord bot creation guide
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — System architecture overview
- [EXTENDING.md](docs/EXTENDING.md) — Adding skills and customizing agents
- [OLLAMA.md](docs/OLLAMA.md) — Running builder/reviewer/summarizer on a local Ollama server
- [UPGRADING.md](docs/UPGRADING.md) — Manual upgrade instructions

## Architecture

Karakos consists of:
- **Agent Server** — Manages Claude subprocess lifecycle, message queue, cost tracking
- **Relay** — Routes Discord messages, dispatches work to builder/reviewer agents
- **Scheduler** — Runs periodic tasks (heartbeats, memory consolidation)
- **Dashboard** — Next.js web interface for monitoring and chat
- **Agents** — Configurable Claude instances with specialized roles

## Core Agents

- **Primary** — Main agent, handles general requests and coordination
- **Relay** — Lightweight monitor, processes heartbeats and system notifications

## Optional Agents

- **Builder** — Code generation agent (invoke-builder.sh)
- **Reviewer** — Adversarial code review agent (invoke-reviewer.sh)

## License

MIT

## Contributing

Contributions welcome! Please open an issue or pull request on GitHub.

---

Built with [Claude Code](https://claude.ai/claude-code) (Anthropic).
