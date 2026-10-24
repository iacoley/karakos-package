#!/usr/bin/env bash
# First-boot and every-boot setup shared by both install modes. Idempotent.
#   bin/native-bootstrap.sh              native: loads config/.env itself
#   bin/native-bootstrap.sh --inherit-env  container: bin/entrypoint.sh already has the
#                                          environment and has validated it
# Run after config/ exists. Does not start any process.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"

INHERIT=0
[ "${1:-}" = "--inherit-env" ] && INHERIT=1
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKSPACE_ROOT"

if [ "$INHERIT" = 0 ]; then
    ENV_FILE="$WORKSPACE_ROOT/config/.env"
    [ -f "$ENV_FILE" ] || { echo "native-bootstrap: $ENV_FILE missing; run ./setup.sh --native first" >&2; exit 1; }
    _root="$WORKSPACE_ROOT"
    set -a; . "$ENV_FILE"; set +a
    # A .env carried over from a container install says WORKSPACE_ROOT=/workspace: the root
    # resolved above wins. Re-source paths.sh with it pinned.
    KARAKOS_HOME="$_root"
    source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"
    cd "$WORKSPACE_ROOT"
    [ -x "$WORKSPACE_ROOT/.venv/bin/python3" ] && export PATH="$WORKSPACE_ROOT/.venv/bin:$PATH"

    for var in DASHBOARD_PORT AGENT_SERVER_TOKEN; do
        [ -n "${!var:-}" ] || { echo "native-bootstrap: $var not set in config/.env" >&2; exit 1; }
    done
fi

# Schema stamp: refuse to start on unstamped (1.x) or too-old data BEFORE anything below
# creates files in data/. Exit 78 with a "run: karakos migrate" message. A genuinely empty
# data dir is a fresh install and is stamped here.
STAMP_VERIFIED=0
if [ "${KARAKOS_SKIP_STAMP_CHECK:-}" != "1" ] || [ "${KARAKOS_ENV:-}" = "production" ]; then
    python3 "$PKG_ROOT/lib/migrate/guard.py" check "$WORKSPACE_ROOT/data" || exit $?
    python3 "$PKG_ROOT/lib/migrate/guard.py" stamp --fresh "$WORKSPACE_ROOT/data" || exit $?
    STAMP_VERIFIED=1
fi

mkdir -p \
    "$WORKSPACE_ROOT/data/messages" \
    "$WORKSPACE_ROOT/data/memory" \
    "$WORKSPACE_ROOT/data/health" \
    "$WORKSPACE_ROOT/logs/agent-streams" \
    "$WORKSPACE_ROOT/logs/session-summaries" \
    "$WORKSPACE_ROOT/inbox"

# Fresh install: create the memory graph once. Only when this run stamped the data dir and
# graph.db is absent, so a migrated 1.x install is never touched.
if [ "$STAMP_VERIFIED" = "1" ] && [ ! -e "$WORKSPACE_ROOT/data/memory/graph.db" ]; then
    (cd "$PKG_ROOT" && python3 -m lib.graph init "$WORKSPACE_ROOT/data") || true
fi

# Inbox dirs for each configured agent. Boot never converts 1.x config: `karakos migrate`
# does. A missing or invalid registry after the stamp is an error.
if ! AGENT_IDS=$(python3 "$PKG_ROOT/lib/registry.py" --workspace "$WORKSPACE_ROOT" ids); then
    echo "ERROR: config/agents.yaml is missing or invalid (see above)." >&2
    exit 1
fi
for agent in $AGENT_IDS; do
    mkdir -p "$WORKSPACE_ROOT/inbox/$agent"
    mkdir -p "$WORKSPACE_ROOT/agents/$agent/inbox"
    mkdir -p "$WORKSPACE_ROOT/agents/$agent/journal"
done

# git repo for the protected-paths pre-commit hook (bounded index, see entrypoint history).
if [ ! -d "$WORKSPACE_ROOT/.git" ]; then
    git -c init.defaultBranch=main init -q
    git -c user.email=karakos@local -c user.name=karakos \
        commit --allow-empty -q -m "Initial commit"
fi

# Regenerate the hooks section of config/claude-settings.json from config/hooks.json.
# Only after the stamp check passed: it writes config files, and boot must never mutate 1.x data.
if [ "$STAMP_VERIFIED" = "1" ]; then
    python3 "$WORKSPACE_ROOT/bin/hooks-sync.py" "$WORKSPACE_ROOT" || true
    # Re-assert the private overlay (overlay/, see docs/OVERLAY.md) after a package upgrade.
    if [ -d "$WORKSPACE_ROOT/overlay/.git" ]; then
        python3 "$WORKSPACE_ROOT/bin/overlay.py" apply --quiet --workspace "$WORKSPACE_ROOT" || true
    fi
fi

# Protected paths git hook.
if [ -f "$WORKSPACE_ROOT/system/check-protected-paths.py" ]; then
    cp "$WORKSPACE_ROOT/system/install-hooks.sh" "$WORKSPACE_ROOT/.git/hooks/pre-commit" 2>/dev/null || true
    chmod +x "$WORKSPACE_ROOT/.git/hooks/pre-commit" 2>/dev/null || true
fi

# Discord slash commands: guild-scoped, safe to re-run, never blocks startup (a 403 means the
# bot was invited without applications.commands; see docs/DISCORD_SETUP.md).
if [ -n "${DISCORD_BOT_TOKEN_PRIMARY:-}" ] && [ -n "${DISCORD_BOT_ID_PRIMARY:-}" ] && [ -n "${DISCORD_SERVER_ID:-}" ]; then
    python3 "$WORKSPACE_ROOT/bin/register-discord-commands.py" || \
        echo "WARNING: Discord slash-command registration failed (see above). The bot will still start." >&2
fi
echo "native-bootstrap: done" >&2
