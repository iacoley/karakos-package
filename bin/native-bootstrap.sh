#!/usr/bin/env bash
# Native mode: the one-time and every-upgrade setup that bin/entrypoint.sh does
# for the container, minus supervisord. Idempotent. Run after config/ exists.
#   bin/native-bootstrap.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"
cd "$WORKSPACE_ROOT"

ENV_FILE="$WORKSPACE_ROOT/config/.env"
[ -f "$ENV_FILE" ] || { echo "native-bootstrap: $ENV_FILE missing; run ./setup.sh --native first" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
[ -x "$WORKSPACE_ROOT/.venv/bin/python3" ] && export PATH="$WORKSPACE_ROOT/.venv/bin:$PATH"

for var in DASHBOARD_PORT AGENT_SERVER_TOKEN; do
    [ -n "${!var:-}" ] || { echo "native-bootstrap: $var not set in config/.env" >&2; exit 1; }
done

# Refuse unstamped or too-old data before creating anything in data/ (exit 78).
python3 lib/migrate/guard.py check "$WORKSPACE_ROOT/data" || exit $?
python3 lib/migrate/guard.py stamp --fresh "$WORKSPACE_ROOT/data" || exit $?

mkdir -p data/messages data/memory data/health logs/agent-streams logs/session-summaries inbox
if [ ! -e data/memory/graph.db ]; then
    python3 -m lib.graph init "$WORKSPACE_ROOT/data" || true
fi

AGENT_IDS=$(python3 lib/registry.py --workspace "$WORKSPACE_ROOT" ids) \
    || { echo "native-bootstrap: config/agents.yaml is missing or invalid" >&2; exit 1; }
for agent in $AGENT_IDS; do
    mkdir -p "inbox/$agent" "agents/$agent/inbox" "agents/$agent/journal"
done

# The protected-paths pre-commit hook needs a git repo.
if [ ! -d .git ]; then
    git -c init.defaultBranch=main init -q
    git -c user.email=karakos@local -c user.name=karakos commit --allow-empty -q -m "Initial commit"
fi

python3 bin/hooks-sync.py "$WORKSPACE_ROOT" || true
if [ -d overlay/.git ]; then
    python3 bin/overlay.py apply --quiet --workspace "$WORKSPACE_ROOT" || true
fi
if [ -f system/check-protected-paths.py ]; then
    cp system/install-hooks.sh .git/hooks/pre-commit 2>/dev/null || true
    chmod +x .git/hooks/pre-commit 2>/dev/null || true
fi

if [ -n "${DISCORD_BOT_TOKEN_PRIMARY:-}" ] && [ -n "${DISCORD_BOT_ID_PRIMARY:-}" ] && [ -n "${DISCORD_SERVER_ID:-}" ]; then
    python3 bin/register-discord-commands.py \
        || echo "WARNING: Discord slash-command registration failed (see above)." >&2
fi
echo "native-bootstrap: done"
