#!/usr/bin/env bash
# Poke Script — Inter-agent and system-to-agent messaging
#
# Usage: poke.sh [OPTIONS] MESSAGE
#
# Options:
#   --agent NAME           Target agent (default: primary agent from config)
#   --source LABEL         Source label shown as author (default: "system")
#   --reply-channel NAME   Channel where agent's response posts (default: "general")
#   --silent               Post to channel_id "0" — agent processes but no Discord post

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-${KARAKOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
AGENT_SERVER_TOKEN="${AGENT_SERVER_TOKEN:-}"
AGENT_SERVER_PORT="${AGENT_SERVER_PORT:-18791}"
CHANNELS_CONFIG="${WORKSPACE_ROOT}/config/channels.json"
AGENTS_CONFIG="${WORKSPACE_ROOT}/config/agents.yaml"
REGISTRY_PY="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib" && pwd)/registry.py"

# Defaults
AGENT=""
SOURCE="system"
REPLY_CHANNEL="general"
SILENT=false
MESSAGE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --agent)
            AGENT="$2"
            shift 2
            ;;
        --source)
            SOURCE="$2"
            shift 2
            ;;
        --reply-channel)
            REPLY_CHANNEL="$2"
            shift 2
            ;;
        --silent)
            SILENT=true
            shift
            ;;
        *)
            MESSAGE="$1"
            shift
            ;;
    esac
done

# Validate message
if [ -z "$MESSAGE" ]; then
    echo "Error: No message provided" >&2
    echo "Usage: poke.sh [OPTIONS] MESSAGE" >&2
    exit 1
fi

# Get primary agent if not specified
if [ -z "$AGENT" ]; then
    if [ ! -f "$AGENTS_CONFIG" ]; then
        echo "Error: Agents config not found: $AGENTS_CONFIG" >&2
        exit 1
    fi
    AGENT=$(python3 "$REGISTRY_PY" --workspace "$WORKSPACE_ROOT" role primary | head -1)
fi

# Get channel ID
CHANNEL_ID="0"
if [ "$SILENT" = false ]; then
    if [ ! -f "$CHANNELS_CONFIG" ]; then
        echo "Error: Channels config not found: $CHANNELS_CONFIG" >&2
        exit 1
    fi
    CHANNEL_ID=$(jq -r ".channels.\"$REPLY_CHANNEL\".id // \"0\"" "$CHANNELS_CONFIG")
fi

# Generate message ID
TIMESTAMP=$(date +%s)
PID=$$
RANDOM_SUFFIX=$(( RANDOM % 65536 ))
MESSAGE_ID="poke-${TIMESTAMP}-${PID}-${RANDOM_SUFFIX}"

# Build payload
PAYLOAD=$(jq -n \
    --arg agent "$AGENT" \
    --arg channel "$REPLY_CHANNEL" \
    --arg channel_id "$CHANNEL_ID" \
    --arg source "$SOURCE" \
    --arg content "$MESSAGE" \
    --arg message_id "$MESSAGE_ID" \
    '{
        agent: $agent,
        channel: $channel,
        channel_id: $channel_id,
        server: "local",
        author: $source,
        author_id: "0",
        is_bot: true,
        content: $content,
        message_id: $message_id,
        mentions_agent: true
    }')

# Spool the payload for bin/flush-deferred-messages.py (scheduler, every 5
# minutes) to re-fire once the server is back (#88). The file is the exact
# /message payload, so a spooled poke and a live poke are indistinguishable
# server-side, and the server's duplicate handling makes refires idempotent.
spool_deferred_message() {
    local deferred_dir="${WORKSPACE_ROOT}/data/deferred-messages"
    mkdir -p "$deferred_dir"
    local deferred_file="${deferred_dir}/$(date +%s)-${MESSAGE_ID}.json"
    printf '%s\n' "$PAYLOAD" > "$deferred_file"
    echo "$deferred_file"
}

# Send to agent server. `|| true`: when the server is down entirely, curl
# exits non-zero and `set -e` would kill the script before the spool branch
# below ever runs; -w still emits http_code 000 in that case.
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST \
    -H "Authorization: Bearer ${AGENT_SERVER_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "http://localhost:${AGENT_SERVER_PORT}/message") || true

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
HTTP_CODE="${HTTP_CODE:-000}"
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "202" ]; then
    echo "Poked $AGENT (message_id: $MESSAGE_ID)"
elif [ "$HTTP_CODE" = "000" ] || [ "$HTTP_CODE" = "429" ] || [ "${HTTP_CODE:0:1}" = "5" ]; then
    # Transient: server down, cost-capped, or erroring. Exit 0 — the message
    # is durably spooled for retry, not lost.
    DEFERRED_FILE=$(spool_deferred_message)
    echo "Agent server unreachable (HTTP ${HTTP_CODE}) — spooled for retry: $DEFERRED_FILE" >&2
else
    # Permanent (bad payload, bad token): a retry returns the same answer.
    echo "Error: HTTP $HTTP_CODE" >&2
    echo "$BODY" >&2
    exit 1
fi
