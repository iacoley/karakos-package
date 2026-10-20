# Agent guide: operating a Karakos install

For a coding agent that has been handed a machine and told to install Karakos,
keep it healthy, improve it and send fixes upstream, with no prior knowledge of
the project. Start with [AGENTS.md](../AGENTS.md) (layout, tests, branches,
protected paths). Everything below uses commands and paths that exist in this
repo; where something does not exist, it says so.

**Two places, don't confuse them.** The *install directory* is a git clone of the
repo on the host (`~/karakos` by default, `KARAKOS_DIR` overrides). The
*container* is the running system; inside it the workspace is `/workspace`.
Only `config/`, `agents/` and `.karakos/` are bind-mounted from the install
directory. `bin/`, `lib/`, `mcp/`, `system/` and the dashboard are baked into
the image, so editing them in the clone changes nothing until you build and run a
dev image ([Self-improve](#self-improve)). Commands below marked *(in container)*
run after `make shell`; the working directory there is `/workspace`.

## Install unattended

**What a human must do first** (no command can): create the Discord bot and
collect its token, bot user ID, server ID, the general and signals channel IDs
and the owner's user ID ([DISCORD_SETUP.md](DISCORD_SETUP.md)). Also have a
Claude login on the host (`claude login`, which needs a browser) or an OAuth
token for `claude_oauth_token`.

**Host requirements:** Docker Engine 24+ with Compose v2 and a running daemon,
`git`, `openssl`, `python3` with PyYAML, `jq`, Node/npm. `install.sh` and
`setup.sh` install what is missing through `apt-get`, `brew`, `dnf` or `pacman`,
using `sudo`; on a host without passwordless `sudo`, install them first.

1. Write the answers file. Copy [answers.example.json](answers.example.json) and edit it.
   Required: `system_name`, `owner_name`, `discord_bot_token`, `discord_bot_id`,
   `discord_server_id`, `channel_general`, `channel_signals`, `owner_discord_id`.
   Optional: `primary_agent_name` (default: `system_name`), `monitor_agent_name`
   (`monitor`), `channel_staff`, `cost_daily_limit` (`25.00`), `cost_monthly_limit`
   (`500.00`), `claude_oauth_token`. Any field can be `"<field>_env": "VAR_NAME"`
   to read the value from an environment variable instead of the file; keep
   tokens that way. Unknown keys, missing fields and malformed values fail with
   one list of every problem before anything is changed (rules: `lib/setup_answers.py`).
   Keep the file out of git.
2. Install, from the release branch:

```bash
export KARAKOS_DISCORD_BOT_TOKEN='...'          # whatever the *_env fields name
curl -fsSL https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.sh \
  | bash -s -- --answers "$HOME/answers.json"
```

   Or from an existing clone: `./setup.sh --answers "$HOME/answers.json"`
   (`KARAKOS_ANSWERS=<file>` works for both). `install.sh` clones the `main`
   branch (a release); `KARAKOS_BRANCH=<branch-or-tag>` overrides it and
   `KARAKOS_DIR`, `KARAKOS_REPO` choose where and from which repo. Setup writes
   `config/.env`, `config/agents.yaml`, `config/channels.json`, pulls the image
   from GHCR and runs `docker compose up -d`. It prints the dashboard password
   once (also in `config/.env` as `DASHBOARD_PASSWORD`); record it.
3. Windows: `install.ps1` has no `--answers`; use WSL with the steps above.

**Verify the install is healthy.** From the install directory:

```bash
make preflight                                   # host checks; names the fix for each failure
make ps                                          # service `karakos` is running (healthy)
set -a; . config/.env; set +a
curl -fsS -H "Authorization: Bearer $AGENT_SERVER_TOKEN" "http://localhost:$AGENT_SERVER_PORT/health"
curl -fsS -H "Authorization: Bearer $AGENT_SERVER_TOKEN" "http://localhost:$AGENT_SERVER_PORT/agents"
curl -fsS -o /dev/null -w '%{http_code}\n' "http://localhost:$DASHBOARD_PORT/login"
docker compose -f config/docker-compose.yml --env-file config/.env exec karakos cat data/.schema-version
```

Healthy means: `/health` reports `"status": "healthy"` with `outbox.pending` 0;
`/agents` lists the primary and monitor agents with `alive: true`; the login page
answers `200`; the stamp names schema 2. Then say hello in the general channel (the
primary should answer within a minute) or use the dashboard's `/chat`. The same
checks, annotated, are in [QUICKSTART.md](QUICKSTART.md#check-that-it-worked).

**Where to look.** `make logs` follows everything (the processes log to stdout).
Files, *in container*: `logs/agent-server.log`, `logs/relay.log`,
`logs/scheduler.log`, `logs/supervisord.log`, `logs/health-alerts.log`,
`logs/blocked-bash.jsonl` (commands the safety hooks denied) and
`logs/agent-streams/<agent>_<timestamp>.jsonl` (raw model stream per agent). Component
heartbeats are `data/health/*.json` and `data/health/agents/<agent>.json`. The full
tree is in [ARCHITECTURE.md](ARCHITECTURE.md#data-layout). The signals channel
gets health alerts (`bin/health-monitor.py` daily, `bin/wedge-check.py` every minute).
For a one-shot, read-only check of the whole install from inside the container, run
`python3 bin/self-diagnose.py` (add `--json` for tools; exit 1 on any warn or critical). `python3 bin/self-test.py` runs that, then calls one
read-only tool on each MCP server and reports PASS or FAIL per step.

## Self-repair

Authority: fixing something broken is yours to do and report. Changing a
setting the owner chose (cost limits, models, schedules, channel routing) or
destroying data (`down -v`, `reset`) is not: ask first. Run the API calls with
`set -a; . config/.env; set +a` loaded. Agent ids come from `GET /agents`.

| Symptom | Where to look | Fix |
|---|---|---|
| Container not starting or restarting | `make ps`; `make logs`; `make preflight`; `docker compose -f config/docker-compose.yml --env-file config/.env config --quiet` | Exit code **78**: the schema-stamp guard refused `data/`. Fresh install: leftovers from an earlier install in the volume; old 1.x data: run `bin/karakos migrate` ([UPGRADING.md](UPGRADING.md)). "`data/`, `logs/` or `inbox/` not writable": root-owned volumes; `make down` then `docker compose ... down -v` **destroys them**, so back up first. Port taken (3000 or 18791): `lsof -i :3000` and change the port in `config/.env`. Missing `~/.claude.json` makes Docker create a directory in its place: remove it and restore the file. Pull fails: the GHCR package is not public or the tag is not published ([QUICKSTART.md](QUICKSTART.md#start-it)). |
| Discord relay not posting | `/health` `outbox` (`pending`, `sending`, `dead`, `oldest_pending_age_s`); *in container* `python3 lib/outbox.py stats`, `list --status dead`, `show <id>`; `logs/relay.log`; `data/health/relay.json` | Replies are written to `data/outbox/outbox.db` first and retried with backoff; a row goes `dead` after 12 tries, 24 h, or a permanent error. Fix the cause (bot token or ids in `config/.env`, bot permissions: [DISCORD_SETUP.md](DISCORD_SETUP.md#troubleshooting)), restart with `make down && make up`, then `python3 lib/outbox.py retry <id>` or `POST /outbox/<id>/retry`. `discard <id>` drops a row for good. Delivery is at-least-once. |
| Agent session stuck or crashed | `GET /agents` (`state`, `alive`, `queue_depth`); `GET /agents/<id>/queue`; `data/health/agents/<id>.json`; `logs/agent-streams/<id>_*.jsonl`; `logs/agent-server.log` | A paused shard looks stuck: Discord `/resume` (`POST /agents/<id>/resume`). Escalate in order, each a `POST` with the bearer token to `http://localhost:$AGENT_SERVER_PORT`: `/agents/<id>/interrupt` (drops the current turn), `/agents/<id>/reload` (respawn, keeps context), `/agents/<id>/flush` (drops queued messages), `/agents/<id>/reset` (new session, **context destroyed**). Discord equivalents: `/interrupt`, `/reload`, `/flush`, `/clear` (= reset), `/status`. There is no CLI for this and no per-process restart command in the repo; for a wedged server use `make down && make up`. A `PROCESSING` agent silent over 120 s triggers a wedge alert in the signals channel. |
| Cost cap hit | `/message` answers **429** `Cost limit exceeded` with `reason` `daily` or `monthly` and `Retry-After: 3600`; `GET /cost`, `GET /cost/<id>`, dashboard `/costs`; the 0.75 warning in signals | Windows are rolling: 24 hours and 30 days. Messages from `OWNER_DISCORD_ID` are exempt, so the owner can still talk. Raising `COST_DAILY_LIMIT` / `COST_MONTHLY_LIMIT` in `config/.env` (then `make down && make up`) is the owner's decision: report the spend and ask. A different pause, "account limit" or token budget, is the usage governor: `GET /usage`, `config/governor.yaml`. |
| Scheduler not firing | `data/health/scheduler.json` (fresh within a minute or two); `logs/scheduler.log`; `data/health/heartbeats/`; `logs/health-alerts.log`; *in container* `ls data/oneshot-spool/` | The scheduler is a supervised process that restarts itself; if the beacon is stale after that, restart the container. There is no host-side watchdog for it (a known gap in [ARCHITECTURE.md](ARCHITECTURE.md#known-gaps)). Times are in the container `TZ` (default UTC). Heartbeats and other machine-started work yield above the thresholds in `config/governor.yaml`. Task cadence: [ARCHITECTURE.md](ARCHITECTURE.md#scheduler-binschedulerpy). Run a task by hand to see whether it works, e.g. `bin/heartbeat.sh <agent-id>` (queues a heartbeat message to that agent). |
| Memory recall empty | *in container* `python3 -m lib.graph status`; `GET /graph/status`; `config/recall-source` and `KARAKOS_RECALL_SOURCE`; `python3 bin/graph-consolidate.py --dry-run` | Check the graph exists and has observations. A `config/recall-source` file or `KARAKOS_RECALL_SOURCE` **replaces** the graph as the source, so a stale one yields nothing: remove it. No embeddings or no `fastembed` degrades to keyword recall (`mode: "keyword"`), which is thinner, not empty. Automated prompts (heartbeats, pokes) skip the per-prompt injection by design. After a 1.x upgrade, confirm the memory step ran (`data/migration-reports/migration-report.md`). Details: [ARCHITECTURE.md](ARCHITECTURE.md#memory). |
| `karakos migrate` failed | The output (it prints the backup path and the restore command); `data/migration-reports/migration-report.md`; the container exits 78 until the stamp exists | A failed step leaves the stamp absent. Fix the cause and run `bin/karakos migrate` again: it restores the backup first (if under 24 hours old), then migrates from the original. To go back: `bin/karakos migrate --restore backups/<dir>`. Rehearse with `bin/karakos migrate --dry-run`. `KARAKOS_VERSION` picks the image the migrator runs. `logs/` and `inbox/` are not in the backup. Full procedure: [UPGRADING.md](UPGRADING.md#the-real-run). |

If none of these fit, read `make logs`, then `logs/agent-server.log`, and write
down what you saw before changing anything. If the cause is a platform bug,
[contribute the fix](#contribute-upstream).

## Managing yourself

The `karakos-admin` MCP tools (`mcp/admin-server.py`) let an agent look at and steer its own runtime:

- `get_usage`: rate-limit headroom for every agent (`GET /usage`).
- `get_cost`: spend for every agent, or one with `agent` (`GET /cost`, `GET /cost/<agent>`).
- `tail_log`: last lines of `logs/<service>.log`, secrets redacted (default 40, max 200).
- `set_effort`: set reasoning effort: low, medium, high, xhigh, max, or default.
- `get_queue`: what an agent still has to answer.
- `pause_agent` / `resume_agent`: hold or release a queue, optionally for 1-1440 minutes.
- `get_findings`: the latest health findings summary.

Self-scoping: `reload_agent`, `reset_agent`, `set_effort`, `pause_agent` and `resume_agent`
act on the calling agent only. A primary agent (`role: primary` in `config/agents.yaml`) or an
operator with no agent identity may act on any agent. Anyone else gets a permission error:
ask the primary agent or the owner.

### Protected paths at edit time

The paths in `config/protected-paths.json` are guarded when you edit them, not only at commit. The
PreToolUse hook `system/hooks/protected-paths-edit.py` runs on `Edit`, `Write`, `MultiEdit`,
`NotebookEdit` and on Bash commands that write through `tee`, `cp`, `mv`, `rm`, `sed`, `install`, `ln`,
`truncate`, `dd`, `chmod`, `chown` or `patch`.

- A tier 1 path is denied with `Protected path (tier 1): <path>. Do not edit it; report to the owner.`
  Do not work around it: tell the owner which path you needed to change and why.
- A tier 2 path is allowed and recorded in `logs/protected-edits.jsonl` (`ts`, `agent`, `tool`, `path`, `tier`, `decision`). Tier 1 denies are recorded there too.
- Paths in `unprotected_overrides` (persona, journal, inbox) are allowed and not logged.
- It applies to agents only (`KARAKOS_AGENT` is set). An operator's own `claude` session is not restricted.
- Symlinks are resolved first, so a link to a protected file is protected. Paths outside the workspace are not.
- Known gap: shell redirections (`echo x > bin/relay.py`) and interpreters that write files themselves (`python -c`, `perl -i`) are not detected. Commit-time protection still applies.

## Self-improve

**Local customisations stay local.** Persona, journals, skills for this household,
agents and their prompts live in `agents/<id>/`, `skills/` and `config/` of the
install and are never sent upstream. **Fixes to the platform itself** (a bug in
`bin/`, `lib/`, `mcp/`, the docs, the installer) go upstream as a PR.

**Builder and reviewer agents.** Not created by default. Create them *in container*:

```bash
bin/create-agent.sh --template builder  --model sonnet builder
bin/create-agent.sh --template reviewer --model sonnet reviewer
```

The builder reads a spec (a markdown file in `inbox/builder/` at the workspace
root), branches, implements, tests and opens a PR; the reviewer critiques a PR or
spec (`bin/invoke-reviewer.sh <spec.md>`) and returns APPROVE, REVISE or RETHINK.
Spec frontmatter reads `target_branch`, `repo`, `branch_prefix`, `requester`
(the Discord ID told when it finishes; without it nobody is notified) and
`callback_channel`. **For a PR to upstream set `target_branch: develop`.** The
templates are `agents/templates/builder.md` and `agents/templates/reviewer.md`;
the full flow is [EXTENDING.md](EXTENDING.md#using-the-builder-agent). The build
queue (`bin/buildq`, `config/build-queue.yaml`) is off by default and optional.

**Adding a skill** (a new tool agents can call):

1. `cp -r skills/hello-world skills/<name>`; edit `skills/<name>/tools.json`
   (`skill_name`, `tools[]` with `name`, `description`, `inputSchema`).
2. Write `skills/<name>/scripts/<tool_name>.py` (or `.sh`). The filename must match
   the tool name. It reads JSON from the `TOOL_ARGS` env var, prints JSON, and is
   killed after 60 s. Its working directory is the skill directory.
3. Test: `python3 mcp/tools-server.py --test-tool <tool_name> '{"arg": "value"}'`.
4. Reset the agent session so the tool server rediscovers it (dashboard → Agents →
   Reset, or `POST /agents/<id>/reset`).

A `SKILL.md` alone does nothing: that is Claude Code's feature, not this one.
Guide: [skills/README.md](../skills/README.md), [EXTENDING.md](EXTENDING.md#adding-a-skill).
Skills for one household belong in that install; one that is generally useful can
be proposed upstream.

**What protected paths block.** `config/protected-paths.json` is enforced by a
pre-commit hook (`system/check-protected-paths.py`). Tier 1 (`system/`,
`config/`, `.karakos/`, `Dockerfile`, `bin/agent-server.py`, `bin/relay.py`,
`bin/entrypoint.sh`, `bin/scheduler.py`, `bin/hooks-sync.py`) cannot be committed
by an agent inside the container workspace, where the hook is installed. A clone on
the host has no hook, so there the rule is yours to keep and PR review is the gate. Tier 2 (rest of `bin/`, `agents/templates/`, `agents/CORE.md`,
`agents/HOUSE_STYLE.md`, `mcp/tools-server.py`) needs owner review. Persona,
journal and inbox files are always writable. If a commit is blocked, stop and
report the path and the change you wanted; do not bypass the hook or edit the
protection list. A change to a tier 1 file reaches users only through a PR a
maintainer reviews.

**Test before proposing.** In a clone of the repo (the install directory is one;
for upstream work use your fork, below):

```bash
python3 -m pytest tests/<the_file_for_what_you_touched>.py -q
python3 -m pytest tests -m "not slow" -q          # the CI unit job, no Docker
bash system/check-coupling.sh                     # "coupling: clean"
bash -n setup.sh install.sh                       # shell syntax
python3 -m py_compile bin/<script>.py             # Python syntax
```

To run a change to `bin/` or `lib/` in a real container, use the dev override
(builds `karakos-dev:local` from the clone; see
[EXTENDING.md](EXTENDING.md#local-development-build)):

```bash
docker compose -f config/docker-compose.yml -f config/docker-compose.dev.yml --env-file config/.env up --build -d
```

Return to the release image with `make down && make up`. Dashboard changes: `cd dashboard
&& npm ci && npm test && npm run build`, or let CI do it.

## Contribute upstream

Branch model: every PR targets **`develop`**. **`main`** holds releases and changes
only through a release PR from `develop` that the maintainer approves; do not open
PRs against it, push to it or tag. Both branches require a PR and passing checks
`lint-and-syntax`, `unit-tests` and `docker-smoke`, and forbid force-push and
deletion. Process and releases: [CONTRIBUTING.md](../CONTRIBUTING.md).

Fixes to the package go upstream as PRs against `develop`. Personal changes (config,
personas, skills) go in the private overlay, never in a fork: see [OVERLAY.md](OVERLAY.md).

You need an authenticated `gh` (`gh auth login`, or `GH_TOKEN` in the environment).

**An install made by `install.sh` is a clone of upstream** (`origin` =
`mcarmody/karakos-package`, branch `main`) and you cannot push there. Fork it and
add the fork as a remote. Check first:

```bash
cd ~/karakos
git remote -v
gh repo fork mcarmody/karakos-package --clone=false --remote=false
GH_USER=$(gh api user -q .login)
git remote add fork "https://github.com/$GH_USER/karakos-package.git"
git fetch origin develop
git switch -c fix/short-description origin/develop
```

(Starting from your fork instead: `gh repo fork mcarmody/karakos-package --clone`, which
names the original remote `upstream`. Substitute `upstream` for `origin` below.)
The install directory carries generated state (`config/.env`, `config/agents.yaml`,
`config/channels.json`, `agents/<id>/`) that is not all git-ignored, so stage files by name; your branch off `develop` does not include it, but a branch
switch in a live install directory changes the files the container mounts. Prefer
a separate working copy for upstream work:
`git worktree add -b fix/short-description ~/karakos-pr origin/develop`.

Then make the change and verify (see [Self-improve](#self-improve)), and:

```bash
git add path/to/changed-file                   # never config/.env, tokens or household names
git commit -m "Short imperative summary of the change"
git push -u fork fix/short-description
gh pr create --repo mcarmody/karakos-package --base develop \
  --head "$GH_USER:fix/short-description" \
  --title "Short imperative summary" --body-file pr-body.md
gh pr checks --repo mcarmody/karakos-package --watch
```

`pr-body.md` follows [.github/pull_request_template.md](../.github/pull_request_template.md):
what changed and why, how it was tested (the commands you ran), and the checklist
(tests pass, no secrets or household data, docs updated). One logical change per PR.

**What CI runs** on a PR to `develop` (`.github/workflows/ci.yml`, `coupling.yml`):
`lint-and-syntax` (Python and shell syntax, file structure, `tests/test_setup.py`),
`unit-tests` (`pytest tests/ -m "not slow"`), `docker-smoke` (builds the image and
checks the dashboard, native modules and dependencies), `dashboard` (`npm test`,
`npm run build`), `preflight`, `compose-check`, and the household-coupling check.
A scheduled run (`real-cli-smoke`) uses the real CLI and needs a repository
secret, so it does not run on fork PRs. Fix red checks with new commits; do not
force-push. The maintainer reviews and merges; do not merge your own PR.
Releases (`develop` → `main`, CHANGELOG, tag, image build) are the maintainer's.
