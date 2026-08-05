#!/usr/bin/env bash
set -euo pipefail

# Overridable so the volume-permission guard below can be tested against a
# temp directory. Always /workspace inside the container.
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"

# Validate required environment variables
required_vars=("DASHBOARD_PORT" "AGENT_SERVER_TOKEN")
missing_vars=()
for var in "${required_vars[@]}"; do
    if [ -z "${!var:-}" ]; then
        missing_vars+=("$var")
    fi
done

if [ ${#missing_vars[@]} -gt 0 ]; then
    echo "ERROR: Required environment variables not set: ${missing_vars[*]}"
    exit 1
fi

# Verify the persistent volumes are writable by the container user before
# anything tries to use them. Docker seeds a named volume's ownership from the
# image only when the volume is FIRST created — an install that predates the
# image's `install -d -o karakos` fix leaves a root-owned volume behind, and
# every later `docker compose up` silently reuses it. `mkdir -p` doesn't catch
# this (the directories already exist), so the first symptom is a supervisord
# PermissionError traceback that says nothing about volumes.
unwritable=()
for dir in "$WORKSPACE_ROOT/data" "$WORKSPACE_ROOT/logs" "$WORKSPACE_ROOT/inbox"; do
    [ -d "$dir" ] && [ ! -w "$dir" ] && unwritable+=("$dir")
done

if [ ${#unwritable[@]} -gt 0 ]; then
    cat >&2 <<EOF
ERROR: Karakos cannot write to its own storage: ${unwritable[*]}

These are Docker volumes left over from an earlier install, and they are owned
by root instead of the container user (uid $(id -u)). Karakos will not start
until they are replaced.

Fix it by deleting the old volumes and starting again. From the config/
directory of your Karakos install:

    docker compose down -v
    docker compose up -d

This erases the message archive and logs in those volumes. Your agents and
configuration live on the host and are not affected.
EOF
    exit 1
fi

# Ensure data directories exist
mkdir -p \
    "$WORKSPACE_ROOT/data/messages" \
    "$WORKSPACE_ROOT/data/memory" \
    "$WORKSPACE_ROOT/data/health" \
    "$WORKSPACE_ROOT/logs/agent-streams" \
    "$WORKSPACE_ROOT/logs/session-summaries" \
    "$WORKSPACE_ROOT/inbox"

# Create inbox dirs for each configured agent
if [ -f "$WORKSPACE_ROOT/config/agents.json" ]; then
    for agent in $(python3 -c "import json; print(' '.join(json.load(open('$WORKSPACE_ROOT/config/agents.json'))['agents'].keys()))"); do
        mkdir -p "$WORKSPACE_ROOT/inbox/$agent"
        mkdir -p "$WORKSPACE_ROOT/agents/$agent/inbox"
        mkdir -p "$WORKSPACE_ROOT/agents/$agent/journal"
    done
fi

# Initialize git if not already (used by the protected-paths pre-commit hook
# which logs/blocks edits to system files made by builder/reviewer agents).
# We bound the index to bin/ + agents/ so we don't try to track the
# bind-mounted node_modules tree, which would take minutes on first boot.
if [ ! -d "$WORKSPACE_ROOT/.git" ]; then
    cd "$WORKSPACE_ROOT"
    git -c init.defaultBranch=main init -q
    git -c user.email=karakos@local -c user.name=karakos \
        commit --allow-empty -q -m "Initial commit"
fi

# Install protected paths git hook
if [ -f "$WORKSPACE_ROOT/system/check-protected-paths.py" ]; then
    cp "$WORKSPACE_ROOT/system/install-hooks.sh" "$WORKSPACE_ROOT/.git/hooks/pre-commit" 2>/dev/null || true
    chmod +x "$WORKSPACE_ROOT/.git/hooks/pre-commit" 2>/dev/null || true
fi

exec supervisord -c "$WORKSPACE_ROOT/config/supervisord.conf"
