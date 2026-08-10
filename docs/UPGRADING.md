# Upgrading Karakos

Manual upgrade instructions. Karakos does not auto-update.

Karakos ships prebuilt multi-arch images to GHCR. The upgrade path is
`docker compose pull && docker compose up -d` — no local build required.

`latest` always tracks the newest release. To control when you upgrade, pin
`KARAKOS_VERSION` in `config/.env` (e.g. `KARAKOS_VERSION=v1.3`). Remove the
pin or update it when you are ready to move to a newer release.

## Version Check

The system checks for updates weekly and posts to #signals if a newer version is available. You can also check manually:

```bash
bin/check-updates.sh
```

## Upgrade Process

### 1. Stop the System

```bash
docker compose down
```

Wait for graceful shutdown (agents finalize sessions, up to 45 seconds).

### 2. Back Up Data

```bash
# Back up the data directory (messages, memory, databases)
cp -r data/ data-backup-$(date +%Y%m%d)/

# Back up config (contains credentials)
cp config/.env config/.env.backup
```

### 3. Pull Updates

```bash
git pull origin main
```

If you have local modifications, stash or commit them first:

```bash
git stash
git pull origin main
git stash pop
```

### 4. Check for Breaking Changes

Read the release notes for your version jump. Breaking changes are documented in the GitHub release.

### 5. Pull and Start

```bash
docker compose pull
docker compose up -d
```

`docker compose pull` downloads the prebuilt image for the new release from GHCR.
No local build step required.

### 6. Verify

1. Check `docker compose logs -f` for startup errors
2. Open the dashboard and verify agent status
3. Check #signals for the startup health report

## Schema Migrations

If the upgrade includes database schema changes, the agent server handles them automatically on startup. It checks the current schema version and applies any pending migrations.

Manual migration (if needed):

```bash
docker exec -it karakos-karakos-1 python3 bin/migrate.py
```

## The Claude CLI Rolls Back On Its Own

Everything above is about upgrading Karakos. The agent loop also runs on the
Claude CLI, which this project does not release and which is replaced on every
`docker compose pull` onto a rebuilt image.

A bad CLI release is the one failure that gives you no signal: it installs
cleanly, `claude --version` answers, every container process stays up, and
messages simply stop being answered.

`bin/cli-upgrade-watchdog.sh` runs at startup and hourly. When the installed
CLI version differs from the last one this install completed a turn on, it
sends one probe message through the CLI. If no answer comes back, it
reinstalls the known-good version and posts a notice to `#signals`. No API
call is made when the version has not changed.

To upgrade the CLI deliberately, behind the same guarantee:

```bash
docker exec -u karakos karakos-karakos-1 bin/upgrade-claude-cli.sh --to 1.2.3
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

The rollback state lives in `data/health/claude-cli.json`. Delete it to make
the watchdog re-adopt whatever is currently installed.

## Rolling Back

If something breaks:

```bash
docker compose down

# Restore data backup
rm -rf data/
mv data-backup-YYYYMMDD/ data/

# Restore config
cp config/.env.backup config/.env

# Check out previous version
git checkout v1.0.0  # or whatever version you were on

# Pull that version's image and start
docker compose pull
docker compose up -d
```

## Version History

Check the GitHub releases page for the full changelog.
