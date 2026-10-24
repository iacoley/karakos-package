#!/usr/bin/env bash
# Cost Report CLI — Show current day/month costs per agent
# Usage: cost-report.sh [--agent NAME]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/paths.sh"
WORKSPACE="$WORKSPACE_ROOT"

AGENT_SERVER_TOKEN="${AGENT_SERVER_TOKEN:-}"
AGENT_SERVER_URL="${AGENT_SERVER_URL:-http://localhost:18791}"

# Parse arguments
AGENT=""
if [ "${1:-}" = "--agent" ] && [ -n "${2:-}" ]; then
    AGENT="$2"
fi

# Query cost endpoint
if [ -n "$AGENT" ]; then
    URL="$AGENT_SERVER_URL/cost/$AGENT"
else
    URL="$AGENT_SERVER_URL/cost"
fi

if [ -z "$AGENT_SERVER_TOKEN" ]; then
    echo "Error: AGENT_SERVER_TOKEN not set" >&2
    exit 1
fi

RESPONSE=$(curl -s -H "Authorization: Bearer $AGENT_SERVER_TOKEN" "$URL")

# Pretty print JSON
echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
