#!/usr/bin/env bash
# discord-notify.sh — Post a message to a Discord channel
#
# Usage:
#   discord-notify.sh general "System update complete"
#   discord-notify.sh signals "⚠️ Agent crashed"

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-${KARAKOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

CHANNEL_NAME="${1:-}"
MESSAGE="${2:-}"

if [[ -z "$CHANNEL_NAME" || -z "$MESSAGE" ]]; then
    echo "Usage: discord-notify.sh CHANNEL_NAME \"message\"" >&2
    exit 1
fi

# Resolve channel name to ID
CHANNEL_ID="$CHANNEL_NAME"
if [[ -f "$WORKSPACE_ROOT/config/channels.json" ]]; then
    RESOLVED=$(python3 -c "
import json
cfg = json.load(open('$WORKSPACE_ROOT/config/channels.json'))
ch = cfg.get('channels', {}).get('$CHANNEL_NAME', {})
print(ch.get('id', '$CHANNEL_NAME'))
" 2>/dev/null || echo "$CHANNEL_NAME")
    CHANNEL_ID="$RESOLVED"
fi

# Get first available bot token
BOT_TOKEN=""
REGISTRY_PY="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib" && pwd)/registry.py"
BOT_TOKEN=$(python3 "$REGISTRY_PY" --workspace "$WORKSPACE_ROOT" legacy 2>/dev/null | python3 -c "
import json, os, sys
cfg = json.load(sys.stdin)
for name, info in cfg.get('agents', {}).items():
    env_var = info.get('discord_bot_token_env', '')
    if env_var:
        token = os.environ.get(env_var, '')
        if token:
            print(token)
            break
" 2>/dev/null || echo "")

if [[ -z "$BOT_TOKEN" ]]; then
    echo "Error: no Discord bot token available" >&2
    exit 1
fi

# Post to Discord
curl -sf -X POST "https://discord.com/api/v10/channels/$CHANNEL_ID/messages" \
    -H "Authorization: Bot $BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg content "$MESSAGE" '{content: $content}')" > /dev/null

echo "Posted to #$CHANNEL_NAME"
