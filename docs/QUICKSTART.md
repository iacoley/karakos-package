# Quick Start

From nothing to an agent answering you in Discord, in about 30 minutes. Most
of that is Discord's bot-creation screens, not this software.

## Before you start

| | |
|---|---|
| **A machine that stays on** | Raspberry Pi 4/5, mini PC, or a VM. 4 GB RAM minimum, 8 GB recommended; 10 GB free disk |
| **OS** | Ubuntu 22.04+, Debian 12+, macOS 12+, or Windows 10/11 (via Docker Desktop + WSL2) |
| **Docker** | Engine 24+ with Compose v2 — check with `docker compose version` |
| **`git` and `openssl`** | Almost always already present. The wizard installs `jq` and Node/npm itself if they're missing |
| **An Anthropic account** | Login happens through the Claude Code CLI. No API key to paste |
| **A Discord bot** | Token, bot user ID, server ID — [DISCORD_SETUP.md](DISCORD_SETUP.md) walks through it |

Do the Discord bot first if you haven't. The wizard will ask for its token
partway through and it is easier to have it ready than to go and get it.

## Install

The one-line installer handles prerequisites, clones the repo to `~/karakos`,
and launches the wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.sh | bash
```

On Windows, in PowerShell as administrator:

```powershell
irm https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.ps1 | iex
```

Or do it by hand — same result:

```bash
git clone https://github.com/mcarmody/karakos-package.git ~/karakos
cd ~/karakos
./setup.sh
```

(`KARAKOS_DIR=/somewhere/else` before the installer changes where it clones.)

The installer clones the `main` branch, which holds releases only. To install
something else (a tag, or `develop` to test unreleased changes), set
`KARAKOS_BRANCH=<branch-or-tag>`; `KARAKOS_REPO=user/repo` points it at a fork.

**Without prompts.** `./setup.sh --answers answers.json` (or
`install.sh --answers answers.json`, or `KARAKOS_ANSWERS=answers.json`) runs the
wizard from a file: [answers.example.json](answers.example.json) lists the fields,
and a secret can be read from an environment variable instead of stored in the file.
It still needs the Discord bot from above and a Claude login (or a
`claude_oauth_token`). Agents installing for you: [AGENT_GUIDE.md](AGENT_GUIDE.md).

## What the wizard asks

Eight steps, plus one prompt inside the third:

1. **System name** — what the installation is called
2. **Owner name** — how the system addresses you
3. **Primary agent name** — defaults to the system name. Right after it the
   wizard asks for the **monitoring agent's name** (default `monitor`), the
   cheap agent that watches the system; it must differ from the primary's
4. **Anthropic login** — opens a browser for `claude login` (OAuth)
5. **Discord bot** — token, bot user ID, server ID
6. **Discord channels** — the general and signals channel IDs
7. **Your Discord user ID** — so it knows which human is the owner
8. **Cost limits** — daily and monthly spend caps

It saves progress as it goes, so you can quit at any prompt and resume by
running `./setup.sh` again. One exception: step 4 is not covered by the resume
state, so a resumed run asks you to log in again.

The credentials `claude login` produces live in `~/.claude/` on the host and
are bind-mounted into the container, so the `claude` CLI inside it inherits
the same session. You do not log in twice.

The wizard prints a dashboard password at the end. Write it down. On a fresh
install it also stamps the empty data directory as schema 2, so there is nothing
to migrate ([UPGRADING.md](UPGRADING.md) is for 1.x installs).

## Start it

**The wizard already started it.** `setup.sh` finishes by pulling the image
and running `docker compose up -d` for you, so if setup completed cleanly,
Karakos is already running and you can skip to the next section.

The `make` targets below are for afterwards — restarts, upgrades, and
day-to-day operation:

```bash
cd ~/karakos
make install     # preflight checks, pull, start
```

`make install` needs `config/.env` to exist, which the wizard creates, so it
is not a substitute for running the wizard first.

Preflight is worth understanding, because it catches the failures that are
otherwise diagnosed an hour later: Docker unreachable, Compose v1 instead of
v2, Docker Desktop's WSL integration not actually running, an unsupported CPU
architecture, shell scripts checked out with CRLF line endings, missing
required env vars, port 3000 already taken, and low disk. It exits non-zero
naming the specific fix.

To run just the checks and start nothing:

```bash
make preflight
```

The image is pulled prebuilt from GHCR (~1.2 GB, multi-arch). There is no
local build step.

**Pinning a version.** `latest` tracks the newest release. To upgrade on your
own schedule, set `KARAKOS_VERSION=v1.3` in `config/.env`. Releases are tagged
`v<major>.<minor>` and `v<major>` — there are no patch-level image tags.

> **Maintainers, first publish only:** GHCR packages are private when first
> created by a `docker push`. After the first `release.yml` run, visit
> [the package page](https://github.com/mcarmody/karakos-package/pkgs/container/karakos)
> → **Package settings** → **Change package visibility** → **Public**, or
> end users cannot pull without authenticating. One time, per package.

## Check that it worked

These commands need no Discord token. They take every port and token from
`config/.env`, so they work whatever ports you chose:

<!-- smoke -->
```bash
set -a; . config/.env; set +a
docker compose -f config/docker-compose.yml --env-file config/.env ps
curl -fsS -H "Authorization: Bearer $AGENT_SERVER_TOKEN" "http://localhost:$AGENT_SERVER_PORT/health"
curl -fsS -o /dev/null -w '%{http_code}\n' "http://localhost:$DASHBOARD_PORT/login"
docker compose -f config/docker-compose.yml --env-file config/.env exec karakos cat data/.schema-version
```

The container is `running`, `/health` returns `"status": "healthy"` with your
agents and an `outbox` with `pending` 0, the login page answers `200`, and the
stamp file names schema 2.

1. **Dashboard** — open `http://localhost:3000` (or your `DASHBOARD_PORT`; the port
   in `config/.env`) and log in as `admin` with the wizard's password. Sessions last 30
   days, and the login works over plain `http://`; behind an HTTPS proxy set
   `KARAKOS_COOKIE_SECURE=1` ([UPGRADING.md](UPGRADING.md#auth-and-env-changes)).
2. **Discord** — say hello in your general channel. The primary agent should
   answer within a minute.
3. **Logs** — `make logs` to watch startup.

If Discord is silent but the dashboard is up, the agent is running and the
Discord side is misconfigured. Start at
[DISCORD_SETUP.md](DISCORD_SETUP.md#troubleshooting).

## The Docker commands, and why they need a flag

The compose file is at `config/docker-compose.yml`, not the repo root, so a
bare `docker compose up` in your checkout finds nothing. The `make` targets
pass the right flags:

| Command | What it does |
|---|---|
| `make up` | Start the container |
| `make down` | Stop it — in-flight turns finish first, up to 45s |
| `make logs` | Follow the log |
| `make shell` | A shell inside the container, as the `karakos` user |
| `make pull` | Fetch a newer image |
| `make help` | List targets |

The long form is
`docker compose -f config/docker-compose.yml --env-file config/.env <command>`.

**There is one service, named `karakos`.** Everything runs inside that single
container, so `docker compose logs relay` or `docker compose logs dashboard`
will not work — those are processes, not services. Their individual logs are
inside the container under `/workspace/logs/`.

## What's actually running

Four supervised processes in the one container:

| Process | Job |
|---|---|
| `bin/agent-server.py` | The core: Claude subprocesses, message queue, cost guard, HTTP API on :18791 |
| `bin/relay.py` | The Discord gateway — carries messages in and replies out |
| `bin/scheduler.py` | Heartbeats, health sweeps, memory consolidation, agent-scheduled one-offs |
| `dashboard` | The Next.js web interface on :3000 |

Plus the MCP tool server, which the Claude CLI starts as its own child.
[ARCHITECTURE.md](ARCHITECTURE.md) has the full picture.

## First things to try

1. Say hello in the dashboard's `/chat` (no Discord needed), or in Discord once
   it is set up.
2. Ask it something that needs a tool — "what's the system health?"
3. Open `/chat` in the dashboard and talk to the same agent there; replies
   stay in the browser rather than posting to Discord.
4. Look at `/costs` after a few exchanges to see what a turn actually costs.
5. Check your signals channel. Agent heartbeats land there every 30 minutes,
   along with cost warnings and health alerts. It is quiet by design — the
   health sweep posts only when something is wrong, so silence there is good
   news, not a broken hook.

## Adding Discord later

The dashboard and its chat work without Discord. To add it later, create the bot
([DISCORD_SETUP.md](DISCORD_SETUP.md)), put its token, bot id, server id and
channel ids in `config/.env` (the names are in `config/.env.template`), and
restart with `make down` then `make up`.

## Adding the coding stack

Builder and reviewer agents are not created by default. To add them:

```bash
make shell
bin/create-agent.sh --template builder  --model sonnet builder
bin/create-agent.sh --template reviewer --model sonnet reviewer
```

The builder picks up specs from `inbox/builder/` at the workspace root — not
from `agents/builder/inbox/` — and opens pull requests; the reviewer reviews
them adversarially. `config/protected-paths.json` decides
what a builder is allowed to touch — see
[ARCHITECTURE.md](ARCHITECTURE.md#protected-paths).

## Stopping

```bash
make down
```

In-flight turns finish before exit, which can take up to 45 seconds.
Data lives in Docker volumes and survives.

## Troubleshooting

**The agent doesn't answer in Discord.**
`make logs` and look for errors from `relay` or `agent-server`. Check the bot
token and channel IDs in `config/.env`. Confirm the bot was invited to the
server with permission to read and send messages in that channel.

**The dashboard won't load.**
Check nothing else holds the port: `lsof -i :3000`. Then `make logs`. Preflight
also checks this — `make preflight`.

**It answered once and now it's quiet.**
Check `/costs` in the dashboard; you may have hit a daily cap. Caps are in
`config/.env` as `COST_DAILY_LIMIT` and `COST_MONTHLY_LIMIT`.

**Costs are higher than you want.**
Lower `COST_DAILY_LIMIT`. The relay agent already defaults to Haiku; the
primary agent's model is set in its config under `agents/`.

**Something is wrong with Docker or WSL.**
`make preflight` names the specific problem and the fix, which beats reading
logs.

**The container exits with code 78.** The schema-stamp guard refused to start.
On a **fresh install** this should not happen: it means `data/` already held files
from somewhere else (an earlier install's leftovers in the volume). Remove the
volume only if you do not need what is in it (`make down`, then
`docker compose -f config/docker-compose.yml --env-file config/.env down -v`).
On an **old (1.x) install** it means the data has not been migrated: follow
[UPGRADING.md](UPGRADING.md); a restart cannot fix it.

**Startup fails complaining that `data/`, `logs/` or `inbox/` is not
writable.** A previous run left root-owned Docker volumes behind. `make down`,
then remove the volumes (`docker compose -f config/docker-compose.yml down -v`)
and start again — this destroys the data in them, so take a backup first if the
install was ever working.

**The agent is down and I cannot reach it.**
The agent API and the agent cannot help you here, so go around them.
Run `make logs` first and read the first error, before you restart anything.
A restart starts a new stream log, so the cause scrolls out of the newest file.
Run `make shell` to get a shell in the container.
The image has no `ps`, so list the agent processes from `/proc`:
`for p in /proc/[0-9]*; do echo "${p#/proc/} $(tr '\0' ' ' < $p/cmdline 2>/dev/null)"; done | grep claude`.
A pid that changes between two runs means a crash loop.
The files `logs/agent-server.log` and `logs/agent-streams/` hold the last output.
From the host, `docker compose -f config/docker-compose.yml --env-file config/.env top karakos` gives the same list, with start times.
Conversation transcripts are in `~/.claude/projects/` on the host.
Do not delete them when a session is locked. The lock is the problem, and the transcripts are the agent's memory.

## Next

- [DISCORD_SETUP.md](DISCORD_SETUP.md) — the bot, in detail
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the system works
- [EXTENDING.md](EXTENDING.md) — your own tools and agents
- [UPGRADING.md](UPGRADING.md) — moving to a new release
