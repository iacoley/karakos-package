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

# Everything else (schema stamp, data dirs, graph init, inbox dirs, git, hooks sync,
# overlay, protected-paths hook, Discord slash commands) is shared with the native
# install: bin/native-bootstrap.sh. This script keeps only what is container-specific.
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$PKG_ROOT/bin/native-bootstrap.sh" --inherit-env

exec supervisord -c "$WORKSPACE_ROOT/config/supervisord.conf"
