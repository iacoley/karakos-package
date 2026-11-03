# Upgrading Karakos

Manual upgrade instructions. Karakos does not auto-update.

Karakos ships prebuilt multi-arch images to GHCR, so there is no local build
step. Moving from 1.x to 2.0 is the one upgrade that also migrates data: it
needs the [drain checklist](#drain-checklist), a backup and
`bin/karakos migrate`. Moves within 2.x are a pull and a restart
([later upgrades](#later-upgrades-within-2x)).

- [Who this is for](#who-this-is-for)
- [What changes for you](#what-changes-for-you)
- [Drain checklist](#drain-checklist)
- [Back up](#back-up)
- [Dry run](#dry-run)
- [The real run](#the-real-run)
- [Start and verify](#start-and-verify)
- [Auth and env changes](#auth-and-env-changes)
- [The env allowlist and registry env](#the-env-allowlist-and-registry-env)
- [What else changed quietly](#what-else-changed-quietly)
- [Rolling back](#rolling-back)
- [Native deployments](#native-deployments)
- [Later upgrades within 2.x](#later-upgrades-within-2x)

`latest` always tracks the newest release. To control when you upgrade, pin
`KARAKOS_VERSION` in `config/.env` (e.g. `KARAKOS_VERSION=v1.3`). Remove the
pin or update it when you are ready to move.

## A note on the Docker commands below

The compose file lives at `config/docker-compose.yml`, not at the repo root,
so a bare `docker compose` run from your checkout will not find it. Every
command here uses the `make` targets, which pass the right flags for you. The
long form, if you prefer typing it, is:

```text
docker compose -f config/docker-compose.yml --env-file config/.env <command>
```

There is one service, named `karakos`. Address it by that name — `docker
compose exec karakos …`, not by a guessed container name.

## Version check

`bin/check-updates.sh` runs weekly (Mondays 05:00) and pokes your signals
channel when a newer release exists, once per release. It compares against
`KARAKOS_VERSION` — the same value `config/docker-compose.yml` uses to pick the
image — so it reports at whatever precision you pinned: on `v1.3` it tells you
about `v1.4`, not about the `v1.3.1` build that is already published under the
`v1.3` tag you are tracking. On the default `latest` it names the new release
and tells you to pull, since there is no version to compare against.

To check on demand:

```bash
bin/check-updates.sh          # --force re-announces a release already seen
```

The notice arrives through `bin/poke.sh`, not a direct webhook post, so an
agent reads the release notes and tells you what changed. That is the opposite
choice from `bin/cli-upgrade-watchdog.sh` below, which bypasses the agent queue
deliberately — the thing *it* reports is that agents cannot answer, which does
not apply here.

## Who this is for

An install that ran Karakos 1.0 to 1.5 and is moving to 2.0. A fresh 2.0 install
skips all of this: the setup wizard stamps an empty data directory and there is
nothing to migrate ([QUICKSTART.md](QUICKSTART.md)).

The 2.0 image will not start on a 1.x data directory. It checks
`data/.schema-version` before touching anything, finds no stamp on a non-empty
directory and **exits 78** with `this data directory is from Karakos 1.x; run:
karakos migrate`. A restart loop cannot fix that, and nothing is damaged by it;
run the migrator. The same exit and message appear when the stamp names an
older schema than the image needs.

The migrator detects three 1.x layouts (the buckets in
[migration-inventory.md](migration-inventory.md)): 1.0 to 1.2, 1.3 to 1.4, and
1.5. They differ in how the old compose file mounted the checkout and in which
tables exist, not in what the migrator does to your data.

## What changes for you

| Area | 1.x | 2.0 |
|---|---|---|
| Layout | Compose could bind-mount the whole checkout (1.0 to 1.2) | `config/`, `agents/` and `.karakos/` are bind-mounted; `data/`, `logs/` and `inbox/` are named volumes. The migrator rewrites `config/docker-compose.yml` and keeps the original as `docker-compose.yml.pre-2.0` |
| Config | `config/agents.json` | `config/agents.yaml`, the registry: roles, shards, `prompt:`, `env:`, `effort`, budgets. `agents.json` is left in place |
| Memory | `memory.db` (the 1.x SQLite store) | One knowledge graph, `data/memory/graph.db`. `memory.db` is renamed `memory.db.migrated` and kept |
| Auth and env | The dashboard read the agent server's whole environment | Subprocesses get an allowlist plus the agent's `env:`; the dashboard cookie settings changed ([Auth and env changes](#auth-and-env-changes)) |
| Outbox | A dead-letter file for failed Discord replies | `data/outbox/outbox.db`: retries with backoff, an audit trail, operator verbs |
| Prompts | `system_prompt:` per agent | The same prompt keeps working; the 2.0 core, house style and templates are opt-in ([What else changed quietly](#what-else-changed-quietly)) |
| Dashboard | An older `dashboard/` tree | A rewritten `dashboard/` tree in this repo: roster, fleet, chat, memory browser, history, costs |

## Drain checklist

Do this while the 1.x stack is still running, and stop the stack only after
every line shows zero. Commands run on the host from your checkout; the
container has no `sqlite3` binary, so database checks use `python3` or the HTTP
API. `DC` below is shorthand for
`docker compose -f config/docker-compose.yml --env-file config/.env`.

```bash
set -a; . config/.env; set +a
AUTH="Authorization: Bearer $AGENT_SERVER_TOKEN"
```

1. **Inbound deferred messages.** Messages spooled while the server was down.
   Zero is an empty directory; `stale/` (too old to re-fire) and `invalid/`
   (unparseable) are records, not work, but read them before you discard
   anything.

   ```bash
   docker compose -f config/docker-compose.yml --env-file config/.env \
     exec karakos ls -A data/deferred-messages
   ```

2. **The outbox.** On a 2.x install, `outbox.pending` and `outbox.sending` in
   `/health` are 0 and `python3 lib/outbox.py stats` agrees. On a 1.x install
   there is no outbox yet: review the dead-letter file
   (`data/discord-dead-letter.jsonl`, if it exists) and decide what each line
   means, because the migrator imports every valid line as a `dead` outbox row
   and renames the file `.migrated`.

   ```bash
   curl -s -H "$AUTH" "http://localhost:$AGENT_SERVER_PORT/health"
   docker compose -f config/docker-compose.yml --env-file config/.env \
     exec karakos python3 lib/outbox.py stats
   ```

3. **In-flight queue rows.** `GET /agents/<id>/queue` lists queued rows
   (`"state": "pending"`) and in-flight ones (`"state": "processing"`). Zero
   `processing` rows for every agent. Rows still `pending` survive the
   migration, so they are not a blocker.

   ```bash
   curl -s -H "$AUTH" "http://localhost:$AGENT_SERVER_PORT/agents/main/queue"
   ```

   To count rows by status for every agent at once (0 queued, 1 in progress,
   2 complete, 3 crashed, 4 skipped):

   ```bash
   docker compose -f config/docker-compose.yml --env-file config/.env exec karakos python3 -c \
     "import sqlite3; c=sqlite3.connect('file:data/memory/agent-server.db?mode=ro',uri=True); print(c.execute('select agent, processed, count(*) from message_queue group by 1,2').fetchall())"
   ```

4. **Scheduled jobs and one-offs.** `python3 bin/oneshot.py list` shows
   pending one-offs; know which ones will fire after the upgrade.
   `data/health/scheduler.json` is the scheduler's own beacon; its timestamp
   should be recent.

   ```bash
   docker compose -f config/docker-compose.yml --env-file config/.env \
     exec karakos python3 bin/oneshot.py list
   ```

5. **Running builders and reviewers.** `inbox/builder` and `inbox/reviewer`
   are empty and no `invoke-*` process is running.

   The image has no `ps`, so the process check reads `/proc`; the bracket in
   the pattern keeps the command from matching itself, and no output means no
   builder or reviewer is running.

   ```bash
   docker compose -f config/docker-compose.yml --env-file config/.env \
     exec karakos sh -c 'ls -A inbox/builder inbox/reviewer; grep -la "invoke-[br]" /proc/[0-9]*/cmdline'
   ```

Then stop the stack: `make down` (graceful, in-flight turns finish first, up to
45 seconds).

## Back up

Copying `./data` from the host backs up nothing: `data/` is a **named Docker
volume**, not a folder in your checkout. The migrator takes its own backup
before it changes anything, and that is the one to rely on (it writes to
`backups/` in your checkout unless you pass `--backup-to DIR`):

- It covers `data/`, `config/` (including `.env`) and `agents/`, with a hashed
  `MANIFEST.json`; SQLite files are copied with the online backup API, so they
  are consistent.
- It does **not** cover the `logs/` and `inbox/` volumes, and the dry run and
  the real run say so. No migration step changes them (the old copies stay where
  they are), and `logs/` can be large. Copy them yourself if you want them:

```bash
docker compose -f config/docker-compose.yml --env-file config/.env \
  cp karakos:/workspace/logs ./logs-backup-$(date +%Y%m%d)
docker compose -f config/docker-compose.yml --env-file config/.env \
  cp karakos:/workspace/inbox ./inbox-backup-$(date +%Y%m%d)
cp config/.env config/.env.backup    # credentials: keep this somewhere safe
```

`.karakos/` is bind-mounted from your checkout; back it up by copying the
directory or committing it. A copy of the checkout is also worth having before
you pull.

## Dry run

Get the 2.0 code and image first. `git pull` does not change which image runs:
the tag comes from `KARAKOS_VERSION`, and releases are tagged `v<major>.<minor>`
and `v<major>` (and `latest`). Two places read it, and they do not read the same
source:

- `make pull` and `make up` read `config/.env`. **Set the pin there to the 2.0
  tag (or remove it) before `make pull`**, or you will start the old image on
  migrated data.
- `bin/karakos` reads your **shell** environment first, then the
  `KARAKOS_VERSION` line in `config/.env` (next to the compose file), then falls
  back to `v2.0`, a tag the release workflow publishes. Export it
  (`export KARAKOS_VERSION=v2.0`) to be explicit.

```bash
git pull origin main
export KARAKOS_VERSION=v2.0      # the 2.0 release tag; also set it in config/.env
make pull
bin/karakos migrate --dry-run --report-to /backups/migration-plan.md
```

`--report-to` runs inside the migrator's container, where only `/backups` maps
to the host: the report lands in `backups/migration-plan.md` in your checkout.

`bin/karakos` is the host wrapper. It runs the in-container migrator
(`bin/karakos-migrate`, which is `python3 -m lib.migrate`) against your
volumes. A dry run writes nothing, leaves the stack as it is, and prints:

- the detected version and layout, with the evidence it used;
- the plan: the steps that will run, in order;
- **data this migration does not recognise**: unknown tables, columns or config
  keys. Without `--force` the real run refuses on any of these (**exit 3**);
- the env report: variables in `config/.env` that no agent's `env:` names (see
  [The env allowlist and registry env](#the-env-allowlist-and-registry-env));
- the reminder that memory has no downgrade.

`--report-to FILE` also writes the report to a file outside the install. Exit 3
means read the list; `--force` proceeds anyway, the unrecognised data stays only
in the retained original files, and `migration-report.md` records it.

All flags of `python3 -m lib.migrate`, as in its `--help`:

| Flag | Meaning |
|---|---|
| `--dry-run` | print the plan; write nothing |
| `--auto` | non-interactive (Docker); `bin/karakos migrate` asks for confirmation unless this is set |
| `--force` | proceed on unknown schema or unrecognised data |
| `--parity-queries N` | memory recall-parity queries (default 50; 0 disables) |
| `--backup-to DIR` | where to write the pre-migration backup (default `<root>/backups`) |
| `--import-from DIR` | old checkout mounted read-only; copy its `logs/` and `inbox/` and any `data/` the volume lacks (1.0 layout) |
| `--keep-bind HOST_DIR` | keep the old host checkout bind-mounted for data, logs and inbox (writes `docker-compose.override.yml`) |
| `--report-to FILE` | with `--dry-run`: write the report here |
| `--to-backup DIR` | restore a backup directory |

`bin/karakos migrate` accepts the same set, except that it spells restore
`--restore DIR` and chooses `--import-from` or `--keep-bind` for you (the default
is to import from the checkout it runs in; pass `--keep-bind` to keep the host
paths mounted instead).

## The real run

```bash
bin/karakos migrate          # with KARAKOS_VERSION exported, as in the dry run
```

It stops the stack, takes the backup, runs the steps, verifies each, and writes
the schema stamp last. The migrator's steps own every mutation of existing data;
boot never changes it. In order: layout (`05_layout`), registry (`10_registry`),
monitor template (`12_monitor`), queue columns (`20_queue`), session columns
(`30_sessions`), rate-limit table (`35_rate_limit`), memory to graph
(`40_memory`), build queue (`50_build_queue`), outbox import (`60_outbox`), and a
final integrity check (`90_stamp`).

- **Idempotent.** A second run on a stamped directory prints `already at schema
  2; nothing to do`.
- **Interrupted or failed runs.** A failed step leaves the stamp absent, prints
  the backup path and the restore command. The next run restores that backup
  first, then starts again from the original. A marker older than 24 hours is
  not restored automatically: the install may have changed since; the message
  tells you the command.
- **Memory recall parity.** Before the cutover, the memory step compares recall
  on the old and new stores (`--parity-queries`); a failure leaves `memory.db`
  untouched.
- **`.pre-2.0` files.** The originals of `config/docker-compose.yml` and
  `config/.env` are kept beside the new ones as `*.pre-2.0`.
- **Report.** `data/migration-reports/migration-report.md` lists what the run
  did and what it left behind.

What the migrator does not do:

- **It only adds `.mcp.json` references to `env:`.** Variables the workspace's
  `.mcp.json` names as `${NAME}` are added to each non-monitor agent's `env:` as
  `NAME: ${NAME}` (names only, never values; existing entries are untouched; the
  dry run lists them). Variables read by skill scripts or hooks are still manual
  ([procedure below](#the-env-allowlist-and-registry-env)).
- **The new compose file still mounts `.karakos`**, on purpose: the owner and
  system name in `.karakos/config.json` are still read (prompt composition and
  the `workspace` MCP tool).
- **`logs/` and `inbox/` are not in the backup** ([Back up](#back-up)); the dry
  run says so.
- **Fixtures cover no-Docker mode.** The wrapper was tested against a stand-in
  for `docker`; a run against a real daemon on real hosts is part of the
  release's host testing, so keep the backup and read the first run's output.

## Start and verify

```bash
make up
make logs
```

1. Watch the log for startup errors. An exit 78 here means the stamp is
   missing: the migrator did not finish.
2. `curl -s -H "Authorization: Bearer $AGENT_SERVER_TOKEN" http://localhost:$AGENT_SERVER_PORT/health`
   (after `set -a; . config/.env; set +a`) shows every agent `alive`, and
   `outbox` with `pending` 0.
3. Open the dashboard and confirm agents show as running.
4. Say something to your agent in Discord and confirm it answers.

If startup fails saying `data/`, `logs/` or `inbox/` is not writable, a
previous run left root-owned volumes behind. That needs
`docker compose -f config/docker-compose.yml --env-file config/.env down -v`,
which destroys those volumes: restore from your backup afterwards.

## Auth and env changes

The dashboard source is `dashboard/` in this repository. Its variables are
read by that code, not by the Python side; the authoritative table is in
[`dashboard/README.md`](../dashboard/README.md#environment-variables).

- **Existing logins stay valid.** The session cookie name (`karakos_session`),
  token format and `SESSION_SECRET` scheme did not change. Do not rotate
  `SESSION_SECRET`.
- **Plain-http installs now log in.** The cookie is `Secure` only when
  `KARAKOS_COOKIE_SECURE` is `1`, `true` or `yes`. Leave it unset for
  `http://localhost` and LAN installs; set it behind an HTTPS proxy or tunnel.
- **Session lifetime** is 30 days unless `SESSION_MAX_AGE_SECONDS` is set
  (`86400` restores the old 24 hours). The cookie is `SameSite=Lax`.
- **Passkeys are not in the package profile.**
- **No token or secret is needed to build the image.** The Dockerfile builds
  `dashboard/` from the checkout ([EXTENDING.md](EXTENDING.md#dashboard-build)).

## The env allowlist and registry env

Agent (`claude`) subprocesses no longer inherit the agent server's whole
environment. They get an allowlist (`PATH`, `HOME`, `USER`, locale and proxy
variables, CA settings, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CONFIG_DIR`, `LC_*`, `XDG_*`; the full list is in `lib/spawn_env.py`),
the agent's `env:` block in `config/agents.yaml`, and the server's own identity
variables. Discord tokens and other secrets stay out unless named.

**The upgrade risk.** A skill, hook or MCP server that read an inherited
variable now fails until its agent's `env:` names it. The symptom is a tool
reporting a missing token or credential. The cause is a variable in
`config/.env` that no agent's `env:` references. The dry run lists them.

**To grant a variable**, add it under the agent in `config/agents.yaml`.
`${NAME}` is resolved from the server environment at spawn, so the secret stays
out of the file:

```yaml
agents:
  main:
    env:
      GITHUB_TOKEN: ${GITHUB_TOKEN}
```

**To find what an agent needs**, open the agent's `.mcp.json` (and the scripts
of its skills and hooks) and note each variable they read; add one line per
variable. The migrator's registry step already adds the `${NAME}` references it
finds in `.mcp.json` (names only); you add what skill and hook scripts read.
Then reload the agent (`/reload`); a changed `env:`
applies at the next spawn.

`KARAKOS_ENV_PASSTHROUGH=1` restores the old inherit-everything behaviour for
debugging, and is refused when `KARAKOS_ENV=production`.

**`AGENT_SERVER_TOKEN` is still passed** to every agent subprocess.
That is a residual, not an oversight: the agent's MCP servers call back into
the agent server with it. Any tool the agent runs can therefore use it to call
every authenticated route of the agent server. What limits it: the server is
published on `127.0.0.1` only, and the monitor agent's tool deny list. A scoped
per-shard credential is a post-2.0 item. See
[ARCHITECTURE.md](ARCHITECTURE.md#credentials).

## What else changed quietly

- **`effort:` is now live.** In 1.x a registry `effort:` key was ignored. In 2.0
  it is passed to the CLI as `--effort`, so an upgraded agent whose registry has
  `effort:` set starts using that level, which can change speed and cost. Remove
  the key to keep the CLI default. `/effort <level>` overrides it at runtime
  (stored in `data/runtime-overrides.json`; the registry file is never edited).
- **The dead-letter file is now the outbox.** Failed Discord replies are retried
  with backoff and audited in `data/outbox/outbox.db`; operator verbs are
  `python3 lib/outbox.py {stats,list,show,retry,discard}` and the `/outbox`
  routes. `discord-dead-letter.jsonl` is imported as `dead` rows and renamed
  `.migrated`. Delivery is at-least-once.
- **Graph memory.** `memory.db` is renamed `memory.db.migrated` and never deleted
  by code; removing it is your decision. **There is no downgrade:** a 2.0 graph
  cannot be turned back into a `memory.db`; the backup is the only way back.
- **A recall script replaces the graph recall.** If your install has its own
  `config/recall-source` (or sets `KARAKOS_RECALL_SOURCE`), it **replaces** the
  graph recall for prompt injection. A script that read `memory.db` now finds
  nothing, because that file was renamed; point it at the graph
  (`data/memory/graph.db`, read through `lib/graph`) or delete `config/recall-source`
  to use the built-in recall.
- **Stream logs before the upgrade are not scrubbed.** From 2.0 the stream-log
  tee, `turn_events` rows and tool lines mask credential-shaped strings and the
  values of secret-named variables (`lib/redact.py`). Existing files under
  `logs/agent-streams/` are not rewritten.
- **Pause and resume.** `/pause [minutes]` and `/resume` hold and release an
  agent's queue (`data/operator-pause.json`).
- **Discord behaviours.** Threads for long turns, reaction notices, edit
  reroute and embed suppression are all **off by default**
  ([DISCORD_SETUP.md](DISCORD_SETUP.md#optional-behaviours)).
- **Upgraded agents keep their own prompt.** The migrator writes each migrated
  agent `prompt: {section: <its old system_prompt path>, core: false,
  house_style: false}`, so the text it already had is not duplicated. The 2.0
  core text and house style are additions you opt into: set `prompt.core` and
  `prompt.house_style` to `true` (and `prompt.section` if you move the file) in
  `config/agents.yaml`, then **reset the agent's session**,
  because a resumed session ignores a changed system prompt. The 2.0 primary
  and monitor templates are likewise opt-in
  ([EXTENDING.md](EXTENDING.md#prompts)).
- **Context handoff.** `context_budget_tokens` and `handoff_on_reset` are
  available; `reset_mode: compact` is inert until `/compact` over stream-json is
  verified against the real CLI, so leave it at `reset`.

## Rolling back

Restore from the migrator's backup. Memory has no downgrade, so this is the only
way back.

```bash
bin/karakos migrate --restore backups/pre-2.0-20261003T120000000000Z
```

Export `KARAKOS_VERSION` first, as for the dry run. The migrator prints this exact command (with the real path of your backup) after every run.
The wrapper stops the stack, verifies the backup's manifest, and puts `data/`,
`config/` and `agents/` back; inside the container the same step is
`python3 -m lib.migrate --to-backup DIR`. Then roll the **image** back, which is
the part that matters: checking out an old git tag does not do it. Set the pin
in `config/.env`, match the checkout to it, and start:

```bash
KARAKOS_VERSION=v1.5         # the release you were on
```

```bash
git checkout v1.5            # match the checkout to the pin
make pull
make up
```

`logs/` and `inbox/` are not part of the backup; restore the copies you took in
[Back up](#back-up) if you need them.

## Native deployments

Docker plus supervisord is the supported deployment. A deployment that runs the
processes natively is **not migrated**: `bin/karakos migrate` needs `config/docker-compose.yml`
and stops with `no compose file at <path>` when it is absent, and the migrator is
tested only against the container layout. The 2.0 code still refuses to start on
an unstamped data directory (exit 78), so a native 1.x install has to move to the
container first. [EXTENDING.md](EXTENDING.md#native-deployments-not-supported-lessons) lists what
a native deployment has to supply.

## The Claude CLI rolls back on its own

Everything above is about upgrading Karakos. The agent loop also runs on the
Claude CLI, which this project does not release and which is replaced on every
image pull.

A bad CLI release is the one failure that gives you no signal: it installs
cleanly, `claude --version` answers, every container process stays up, and
messages simply stop being answered.

`bin/cli-upgrade-watchdog.sh` runs at startup and hourly. When the installed
CLI version differs from the last one this install completed a turn on, it
sends one probe message through the CLI. If no answer comes back, it
reinstalls the known-good version and posts a notice to your signals channel.
No API call is made when the version has not changed.

To upgrade the CLI deliberately, behind the same guarantee:

```bash
docker compose -f config/docker-compose.yml --env-file config/.env \
  exec -u karakos karakos bin/upgrade-claude-cli.sh --to 1.2.3
```

It records the current version before touching anything, verifies the new one
can complete a turn, and reverts if it cannot. Exit codes: `0` upgraded and
verified, `1` reverted, `2` reverted and the revert failed too, `3` refused to
run.

To confirm the guard is armed without breaking your CLI to find out:

```bash
bin/cli-upgrade-watchdog.sh --selftest
bin/upgrade-claude-cli.sh --selftest
```

Both run entirely against fake `npm` and `claude` binaries and change nothing.

The rollback state lives in `data/health/claude-cli.json` inside the
container. Delete it to make the watchdog adopt whatever is installed now.

## Later upgrades within 2.x

Once on 2.0, an upgrade is a pull and a restart: the stamp is already current,
and a newer image that needs a newer schema exits 78 with `run: karakos migrate`
until you run it.

```bash
make down
git pull origin main
make pull
make up
```

Back up first (`bin/karakos migrate` does it for you when a migration is due),
read the release notes for the version you are moving to, and bump
`KARAKOS_VERSION` if you pinned it.

### Self-upgrade

A Docker install can upgrade on request from inside. The container has no
Docker socket (on purpose), so a small agent on the host does the work.

One time, on the host, from the checkout:

```bash
make install-upgrade-agent
```

This adds a per-minute line to your crontab that runs `bin/upgrade-agent.sh`
(`make install` does it too; `make uninstall-upgrade-agent` removes it). After
that an agent upgrades with:

```bash
bin/self-upgrade.sh request v2.5.0     # latest | vX.Y | vX.Y.Z; default latest
bin/self-upgrade.sh status             # state of the current or last upgrade
```

`request` writes `.karakos/upgrade/request.json` and `status.json` in the
bind-mounted `.karakos/` directory. It refuses (exit 3) while a request is
pending or running, and exits 4 with a warning if the host agent has not run in
the last 10 minutes (the request stays queued).

Each minute the host agent touches `.karakos/upgrade/host-agent.seen`, takes a
lock, and, if a request is pending:

1. tags the running image `ghcr.io/mcarmody/karakos:rollback`;
2. runs `git pull --ff-only origin main` (a dirty tree or non-fast-forward
   fails the upgrade before anything is touched);
3. sets `KARAKOS_VERSION` in `config/.env` to the requested version (for
   `latest` it leaves an existing pin alone and says so in `status.json`);
4. runs `make pull`, `make down`, `make up`;
5. polls the container for up to 180 s until it is `healthy`.

On success `status.json` says `done`. On any failure it sets `KARAKOS_VERSION`
to `rollback`, runs `make up` to bring the previous image back, and writes
`failed` with the failing step in `detail` (the old value is recorded there;
set `KARAKOS_VERSION` back to a version when you want to move on). Everything is
logged to `.karakos/upgrade/host.log`.

**Migrations are never automatic.** If the new image exits 78 (`run: karakos
migrate`), the host agent rolls back and writes `needs-migrate`. Run
`karakos migrate` yourself, then request the upgrade again.

On the next container start the entrypoint runs `bin/self-upgrade.sh report`,
which pokes the requesting agent with the result once.

## Version history

The full changelog is on the GitHub releases page and in
[CHANGELOG.md](../CHANGELOG.md).
