#!/usr/bin/env bash
# Heartbeat Script — Periodic health check and task reminder for agents

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-${KARAKOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
AGENT="${1:-}"

if [ -z "$AGENT" ]; then
    echo "Usage: heartbeat.sh AGENT_NAME" >&2
    exit 1
fi

TIMESTAMP=$(date '+%H:%M')
MESSAGE="[HEARTBEAT] ${TIMESTAMP} — Check system health, inbox, and pending tasks."

"${WORKSPACE_ROOT}/bin/poke.sh" \
    --agent "$AGENT" \
    --source "heartbeat" \
    --reply-channel signals \
    "$MESSAGE"
