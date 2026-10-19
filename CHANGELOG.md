# Changelog

All notable changes to Karakos are recorded here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note on historical tags.** Releases before `v1.5.0` used two tag forms —
> `vX.Y` and `vX.Y.Z` — and `v1.1` was created *after* `v1.1.1`, so the tag
> order does not match the history. Those tags are left as they are, because
> rewriting published tags breaks anyone pinned to them. From `v1.5.0`
> onward, every release is a three-part `vX.Y.Z`. Entries below `v1.4.1` are
> reconstructed from commit history and are less granular than entries going
> forward will be.

## [Unreleased]

### Added

- Friction sensor (no LLM): `lib/friction.py`, `bin/friction-sensor.py` and an hourly `friction-sensor` job scan the agent stream logs for tool errors, non-zero Bash exits, denials, rate-limit pressure and repeated commands, append to `data/friction-log.jsonl` and refresh `data/friction/weekly.md`. Read-only: nothing edits a prompt or persona. (Refs #237, build A; the nightly review and `friction` MCP action are build B)
- `bin/self-test.py`: runs `bin/self-diagnose.py`, then starts both MCP servers over stdio and calls one read-only tool on each; PASS/FAIL per step, `--json`, exit 0/1. (Refs #236, part 2; builds on the `bin/self-diagnose.py` PR)
- `bin/self-diagnose.py`: read-only whole-install diagnosis (agent server health, outbox, monitor findings, job heartbeats, pre-commit hook, disk, schema stamp, log errors), ranked output, `--json`, exit 0/1. (Refs #236, part 1; the admin MCP tool, `--self-test` canary, build-queue and version-drift sections are follow-ups)

## [2.1.0] - 2026-10-19

### Changed

- Agents see where each message was posted: the batch line carries the channel name, channel id and server (Discord guild name, or `dm`), printed once per run of same-channel rows. A thread reads as `parent/thread`. Channels missing from `channels.json` show their Discord name instead of `unknown`. Queue rows for Discord messages now store the guild name in `server` rather than `discord`; `server` is also what the edit-reroute lookup matches on, so the relay's edit and reaction paths send the same value.

### Added

- Private overlay repo for per-install data: `overlay/` is its own git repo holding config, personas and skills (named seams), applied over the package so installs never fork it; `bin/overlay.py` runs `init`, `status`, `apply`, `capture`, `pull`, `push` and `check-secrets`. (Refs #243, "MÍRDAIN G12: a private overlay repo for per-install data, so installs never fork the package")
- Memory hygiene: `memory` gains `forget` and `supersede`, `graph` gains `delete_entity`, `delete_edge` and `merge_entities`, `taskboard` gains `delete`. All are soft deletes with a `memory_audit` row (tombstone for tasks); hard delete stays operator-only. (Refs #238, build A; the dedup script is build B and not included)
- Per-channel `ux.tool_streaming` in `channels.json`: a channel can turn the tool-activity lines on or off, overriding the agent's `tool_streaming`; unset inherits the agent setting. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")

### Fixed

- Fresh install initialises the memory graph at first boot (`data/memory/graph.db`), so `memory` and `graph` tools no longer report uninitialised; the not-initialised message names both `python3 -m lib.graph init` (fresh install) and `karakos migrate` (1.x upgrade). Fixes #227 ("Fresh install leaves memory graph uninitialised"). (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- Rate-limit warning says "of the window's time elapsed" instead of "through the window", so elapsed time is no longer read as usage. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- Agent server exits 0 on SIGTERM/SIGINT without a "Task exception was never retrieved" traceback: the shutdown handler no longer raises `SystemExit` inside a task. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- `preflight.sh` passes the Docker engine, Compose v2 and WSL integration checks inside the container (`/.dockerenv` or `KARAKOS_IN_CONTAINER=1`) instead of failing on host-only tooling. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- `workspace status` reports the real release: `installed_version` (and `version`) comes from the root `VERSION` file, and `schema_version` from `data/.schema-version`; `VERSION` is in the image and the release process sets it. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")

### Removed

- The `discord` tool's `online` action. It never worked: the agent environment holds no Discord token. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")

## [2.0.2] - 2026-10-19

### Fixed

- Relay refreshes `relay.json` every 60 s from a background task, so the monitor's 300 s staleness check no longer pages on a quiet relay; the heartbeat timestamp is now UTC. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- MCP tools server refreshes `mcp-tools.json` every 120 s while it runs (not in `--test-tool` mode), so the 600 s staleness check no longer pages on a quiet server. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- Drift check no longer reports `job-late` for a job whose two most recent runs straddle a scheduler restart. (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- `register-discord-commands.py` sends a `User-Agent` header, so slash-command registration no longer fails with HTTP 403 "error code: 1010". (Refs #228, "v2.0.0 fresh-install defects from Babybel live-fire check")
- `wait-for` no longer times out early when the clock reads a whole second. (PR #226)

## [2.0.1] - 2026-10-12

### Added

- `./setup.sh --answers <file.json>` (also `install.sh --answers` and `KARAKOS_ANSWERS`) runs the wizard without prompts, with secrets readable from environment variables; example in `docs/answers.example.json`.
- `AGENTS.md` and `docs/AGENT_GUIDE.md`, an entry point and operating guide for coding agents (install, self-repair, self-improve, contribute upstream). `CLAUDE.md` imports `AGENTS.md`.

### Changed

- `install.sh` and `install.ps1` clone `main` explicitly; `KARAKOS_BRANCH` overrides it.
- Branch model: PRs target `develop`; `main` takes releases only through a release PR. CI and the coupling check also run on PRs to `develop`. `CONTRIBUTING.md` rewritten, with a release process section.

### Fixed

- `session` tool `load_last` now returns an explicit error when no agent identity is available (`KARAKOS_AGENT` unset and no `agent` argument) and rejects path-like agent names, instead of guessing (Fixes #160, "session tool: load_last can return another agent's summary").
- `bin/purge-data.py` derives the agent for session-summary retention by stripping the trailing timestamp, so hyphenated agent names such as `test-agent` get their own budget (Refs #156, "purge-data: hyphenated agent names share one session-summary budget"; the VACUUM-count part is not addressed here).

## [2.0.0] - 2026-10-03

2.0 is a new runtime under the same install: an agent registry, shards and a hive, graph memory, a
Discord outbox, steering, a build queue, and a migrator that upgrades a 1.x install in place.
Upgrade with `bin/karakos migrate` ([docs/UPGRADING.md](docs/UPGRADING.md)); it backs up first and
`karakos migrate --restore <backup>` goes back.

Verified for this release: CI unit suite, fresh-install and Docker smokes, and container upgrades
from v1.3 and v1.5.0 with restore. The real-CLI smoke (`pytest tests -q -m "slow and realcli"`)
passed against the real `claude` CLI on the release commit. Upgrades from v1.0.0 and v1.1.1 are not
container-tested (those tags cannot build their own images); the migrator detects and migrates
their layout, covered by unit fixtures. The Windows installer is unchanged from 1.5.

- Fixed: a respawned agent (reload, interrupt, crash recovery) restarted its existing session with
  `--session-id`, which the real CLI refuses ("Session ID is already in use"); it now uses
  `--resume`. A turn whose CLI died before answering is recorded as crashed, not as complete with
  an empty reply, and a message sent while an agent is restarting waits for the new process or
  returns to the queue, so neither is dropped silently.
- Fixed: `karakos migrate --restore` failed with EPERM copying file times onto files the container
  user does not own, and did not remove files the failed run had created.
- The release gate no longer requires a `CLAUDE_CODE_OAUTH_TOKEN` secret: without it the real-CLI
  smoke skips with a notice.

- Documentation for 2.0 (ANDURIL 7.2): UPGRADING rewritten around `bin/karakos migrate` with a drain checklist, ARCHITECTURE restructured for the registry, shards, hive, graph memory, outbox and credentials, new EXTENDING sections (shards, hive, memory, prompts, build queue, upgrade seams, native-deployment lessons), a QUICKSTART with smoke-tagged check commands, `docs/TEST_RESULTS.md` marked historical, and `tests/test_docs.py` to keep them honest.

- Operator switches (ANDURIL 6.4): `/pause [minutes]` holds an agent's queue (the turn in progress finishes; `data/operator-pause.json`, survives restarts; `POST /agents/{name}/pause|resume`), `/resume`, `/effort <level>` (agent-level `--effort` override in `data/runtime-overrides.json`, applied after the turn when busy; `POST /agents/{name}/effort`), and `/interrupt` with an optional `message`. `GET /agents` shards gain `effort`/`effort_source`. A registry `effort:` key, previously inert, is now passed to the CLI as `--effort`.
- Turn logs are redacted: the stream-log tee, `turn_events` rows and tool lines mask credential-shaped strings and the values of secret-named environment variables (`lib/redact.py`). Existing logs are not rewritten (ANDURIL 6.4).

- Removed the unused Go installer source (`installer/`); `install.sh` and `install.ps1` are the install paths.

- The package image builds `karakos-dashboard` at the commit pinned in `dashboard.ref` (`KARAKOS_PROFILE=package`) instead of shipping its own `dashboard/` tree, which is deleted. Node is one pinned major (`NODE_MAJOR`) for both stages. Releases attach a pruned dashboard build bundle (no source). `bin/fetch-dashboard.sh`, `bin/build-dashboard-bundle.sh`, `bin/dashboard-stage.sh` (ANDURIL 5.3).

- The dashboard source is `dashboard/` in this repo again (ANDURIL 5.3b), the package-relevant part of the 2.0 dashboard with its tests; the image builds it with `npm ci` and `next build`. The pinned-ref fetch, source tarball and release bundle (`dashboard.ref`, `bin/fetch-dashboard.sh`, `bin/build-dashboard-bundle.sh`, `bin/dashboard-stage.sh`) and the `DASHBOARD_FETCH_TOKEN` secret are removed; CI and releases need no secret.

- Rate-limit breaker (account-wide, `rate_limit_state` re-keyed by window type via migrator step `35_rate_limit`), per-agent `token_budget_4h` with a 30-minute minimum pause, and a weekly-usage governor for machine-started work (`config/governor.yaml`). `GET /usage` and `GET /agents` gain additive fields (ANDURIL 2.7).

- Build queue (off by default, `config/build-queue.yaml`): `bin/buildq`, per-host concurrency, an admission probe, remote exec hosts over ssh, cancel with salvage, and an outcome check that fails a build with no PR (`lib/buildq.py`, `lib/build_dispatcher.py`; migrator step `50_build_queue`; ANDURIL 3.3).

### Added

- Safety hooks (`bash-safety-rails.py`, `block-bare-ssh.py`, opt-in `block-heavy-build.py`) wired by `bin/hooks-sync.py` from `config/hooks.json`, and a secrets pre-commit (`system/check-secrets.py`). Removed the `rewrite-sleep-poll.py` hook (`bin/wait-for.sh` stays).
- `agent-server.py`: load stored facts from `memory.db` (and candidate facts from `data/memory-candidates/`) and routing-table `MEMORY.md` into `--append-system-prompt` at startup and session resume, closing the durable memory retrieval loop.

### Fixed

- Memory maintenance: episodes created in the nightly pass were pruned in
  the same run before a grace period could apply, a scoring failure
  defaulted to a below-cutoff score, and decay compounded across nightly
  runs instead of applying idempotently. Added `MEMORY_PRUNE_GRACE_DAYS`
  (default 7), a scoring retry, and a `base_importance` column that decay
  is now computed from.
- `memory` MCP tool: added a `remember` action, the first live write path
  into the `facts` table.

## [1.5.0] — 2026-09-23

45 commits since `v1.4.1` (2026-08-04). The first release since the GHCR image
went stale: everything below reaches one-liner installs with this tag. Image:
`ghcr.io/mcarmody/karakos:v1.5` (also `:v1` and `:latest`); pin with
`KARAKOS_VERSION=v1.5`.

### Added

- Scheduler: an agent can schedule future work that survives a restart (#132)
- Discord slash commands, registered on startup (#86, #116, #134)
- `AskUserQuestion` gets a Discord surface (#101, #135)
- Discord attachments are delivered to the agent (#127)
- Dead-letter queue for undeliverable replies (#124)
- Rate-limit headroom tracking, not just dollar spend (#128)
- Health detection for an agent that is alive but wedged (#129)
- Inbound messages spool when the agent server is unreachable (#88, #139)
- Mid-turn tool activity surfaced in the channel (#91, #143)
- Turn activity indicator at the bottom of the turn (#76)
- A queued channel is told it was heard, and drained when the turn ends (#121, #142)
- Unexpected subprocess respawns are announced instead of silently forgotten (#90, #141)
- A banner is rendered when the agent crashed mid-turn (#64, #140)
- Weekly update check that runs, and reports what it finds (#158)
- Roll back a Claude CLI upgrade that cannot complete a turn (#106, #133)
- Relay accepts messages from more than one Discord server (#82)
- Relay reply gate for shared channels, plus a bot-to-bot turn cap (#123)
- `/clear`, `/reload` and `/status` handled directly in the relay (#122)
- Hooks: recall re-injection, reviewable permissions/env (#120); wait-for
  primitive, symlink and sleep-poll PreToolUse hooks, deferred-work Stop hook (#119)
- Dashboard: theming, live turns, conversation metrics, PWA support
- `LICENSE`, `CONTRIBUTING.md` and a PR template for public sharing
- Jekyll config for the GitHub Pages landing site
- Public landing page, sitemap and SEO config (#163)
- `docs/production-grade-plan.md` and this changelog (#163)

### Fixed

- `memory.recall` now actually uses the stored embeddings (#149, #159)
- Every dashboard call hits a route the agent server registers (#151, #161)
- The agent stream log the session summarizer reads is now written (#148, #157)
- `purge-data` and capture point at the databases that actually exist (#150, #155)
- Settings page renders agent config, not runtime state (#130)
- Chat page reads the agent dropdown as an array, not a dict (#125)
- `crash_recovery`'s unposted sweep commits per message (#126)
- `stderr_reader` tasks are cancelled on kill and respawn (#111)
- `--settings` is wired on the `claude` spawn line (#94, #114)
- system-tools registered at repo root; skill discovery depth fixed (#83, #84, #112)
- Duplicate `PreToolUse`/`Stop` entries removed from `claude-settings.json` (#153, #154)
- The install path in the docs matches the software (#147)
- Route tests no longer pass five deleted routes while failing a reformat
- Half the health checks could never have passed; they can now

### Changed

- `test(kara)`: weak assertions replaced with real behavior coverage (#113)
- `preflight`: a `--verify-gates` negative control for every check (#117)
- Issues require an install-visible acceptance test (#109, #115)
- Docs distinguish Karakos skills from Claude Code Agent Skills; root
  `CLAUDE.md` added (#118)
- `ARCHITECTURE.md`: fixed gaps retired, two new ones recorded (#162)
- CI runs once a day, on pull requests and on manual dispatch, instead of on
  every push to `main`
- README: a value-proposition paragraph above the fold (#138)

## [1.4.1] — 2026-08-04

### Fixed

- A root-owned leftover volume no longer kills startup with an unhelpful error.

## [1.4.0] — 2026-06-18

### Fixed

- Purge retention test uses a relative date, so it stops failing with time (#79).

## [1.3] — 2026-04-30

### Fixed

- Container shell scripts tolerate CRLF line endings.

## [1.2] — 2026-04-09

### Changed

- Removed the redundant `docker compose` launch from both installers.

## [1.1] — 2026-04-09

### Changed

- Replaced API-key auth with `claude login`.

> Tagged after `v1.1.1` despite the lower version number.

## [1.1.1] — 2026-04-08

### Fixed

- `.gitignore` no longer excludes `dashboard/lib/`, which broke auth verification.

## [1.1.0] — 2026-04-08

### Fixed

- Windows bash detection in the installer.

## [1.0.0] — 2026-03-30

Initial release: an installable multi-agent household system — Discord
integration, local dashboard, episodic memory, session persistence, cost
tracking, and builder/reviewer agents behind guardrails.

[Unreleased]: https://github.com/mcarmody/karakos-package/compare/v2.1.0...develop
[2.1.0]: https://github.com/mcarmody/karakos-package/compare/v2.0.2...v2.1.0
[2.0.2]: https://github.com/mcarmody/karakos-package/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/mcarmody/karakos-package/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/mcarmody/karakos-package/compare/v1.5.0...v2.0.0
[1.5.0]: https://github.com/mcarmody/karakos-package/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/mcarmody/karakos-package/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/mcarmody/karakos-package/compare/v1.3...v1.4.0
[1.3]: https://github.com/mcarmody/karakos-package/compare/v1.2...v1.3
[1.2]: https://github.com/mcarmody/karakos-package/compare/v1.1...v1.2
[1.1]: https://github.com/mcarmody/karakos-package/compare/v1.1.1...v1.1
[1.1.1]: https://github.com/mcarmody/karakos-package/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/mcarmody/karakos-package/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mcarmody/karakos-package/releases/tag/v1.0.0
