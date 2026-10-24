#!/usr/bin/env bash
# create-agent.sh — Create a new agent at runtime
#
# Usage:
#   create-agent.sh --template primary --model sonnet oracle
#   create-agent.sh --template builder --ephemeral temp-builder

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-${KARAKOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
AGENT_SERVER="http://127.0.0.1:${AGENT_SERVER_PORT:-18791}"
AGENT_SERVER_TOKEN="${AGENT_SERVER_TOKEN:-}"

TEMPLATE="primary"
MODEL="sonnet"
DISCORD_TOKEN=""
EPHEMERAL=false
MAX_TURNS=200

while [[ $# -gt 0 ]]; do
    case "$1" in
        --template)       TEMPLATE="$2"; shift 2 ;;
        --model)          MODEL="$2"; shift 2 ;;
        --discord-token)  DISCORD_TOKEN="$2"; shift 2 ;;
        --ephemeral)      EPHEMERAL=true; shift ;;
        --max-turns)      MAX_TURNS="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: create-agent.sh [OPTIONS] AGENT_NAME"
            echo ""
            echo "Options:"
            echo "  --template NAME        Base template: primary, monitor, builder, reviewer (default: primary)"
            echo "  --model MODEL          Claude model: opus, sonnet, haiku (default: sonnet)"
            echo "  --discord-token TOKEN  Discord bot token (optional)"
            echo "  --ephemeral            Don't persist to agents.yaml"
            echo "  --max-turns N          Max agentic turns (default: 200)"
            exit 0
            ;;
        *)
            if [[ -z "${AGENT_NAME:-}" ]]; then
                AGENT_NAME="$1"
                shift
            else
                echo "Error: unexpected argument: $1" >&2
                exit 1
            fi
            ;;
    esac
done

if [[ -z "${AGENT_NAME:-}" ]]; then
    echo "Error: agent name required" >&2
    echo "Usage: create-agent.sh [OPTIONS] AGENT_NAME" >&2
    exit 1
fi

# Validate name (lowercase, alphanumeric + hyphen)
if [[ ! "$AGENT_NAME" =~ ^[a-z][a-z0-9-]*$ ]]; then
    echo "Error: agent name must be lowercase alphanumeric (got: $AGENT_NAME)" >&2
    exit 1
fi

# `relay` was the 1.x name of the monitor template.
if [[ "$TEMPLATE" == "relay" ]]; then
    echo "Note: template 'relay' is now 'monitor'; using monitor." >&2
    TEMPLATE="monitor"
fi

# Check template exists
TEMPLATE_PATH="$WORKSPACE_ROOT/agents/templates/$TEMPLATE.md"
if [[ ! -f "$TEMPLATE_PATH" ]]; then
    echo "Error: template not found: $TEMPLATE_PATH" >&2
    echo "Available templates: $(ls "$WORKSPACE_ROOT/agents/templates/" | sed 's/.md$//' | tr '\n' ' ')" >&2
    exit 1
fi

# Check for name conflict (config/agents.yaml via lib/registry.py)
SCRIPT_REAL="$(readlink -f "${BASH_SOURCE[0]}")"
REGISTRY_LIB="$(cd "$(dirname "$SCRIPT_REAL")/../lib" && pwd)"
AGENTS_YAML="$WORKSPACE_ROOT/config/agents.yaml"
if [[ "$EPHEMERAL" == "false" ]]; then
    if [[ ! -f "$AGENTS_YAML" ]]; then
        echo "Error: $AGENTS_YAML not found; if this install predates 2.0 run: karakos migrate" >&2
        exit 1
    fi
    EXISTING=$(python3 "$REGISTRY_LIB/registry.py" --workspace "$WORKSPACE_ROOT" ids 2>/dev/null || echo "")
    if echo "$EXISTING" | grep -qxF "$AGENT_NAME"; then
        echo "Error: agent '$AGENT_NAME' already exists" >&2
        exit 1
    fi
fi

echo "Creating agent: $AGENT_NAME (template=$TEMPLATE, model=$MODEL)"

# Create directory structure
AGENT_DIR="$WORKSPACE_ROOT/agents/$AGENT_NAME"
mkdir -p "$AGENT_DIR/persona" "$AGENT_DIR/inbox" "$AGENT_DIR/journal"

# Load system config
SYSTEM_NAME="${SYSTEM_NAME:-Karakos}"
OWNER_NAME="${OWNER_NAME:-User}"

# Build channel list
CHANNELS=""
CHANNELS_JSON="$WORKSPACE_ROOT/config/channels.json"
if [[ -f "$CHANNELS_JSON" ]]; then
    CHANNELS=$(CHANNELS_JSON="$CHANNELS_JSON" REGISTRY_LIB="$REGISTRY_LIB" WORKSPACE_ROOT="$WORKSPACE_ROOT" python3 - <<'PY' 2>/dev/null || echo "- #general"
import json, os, sys
cfg = json.load(open(os.environ['CHANNELS_JSON']))
reg = None
try:
    sys.path.insert(0, os.environ['REGISTRY_LIB'])
    import registry
    reg = registry.load_registry(os.environ['WORKSPACE_ROOT'])
except Exception:
    pass
for name in cfg.get('channels', {}):
    shard = reg.shard_for_channel(name) if reg else None
    default = shard.agent if shard else ''
    print(f'- #{name}' + (f' (default: {default})' if default else ''))
PY
)
fi

# Build other agents list
OTHER_AGENTS=""
if [[ -f "$AGENTS_YAML" ]]; then
    OTHER_AGENTS=$(REGISTRY_LIB="$REGISTRY_LIB" WORKSPACE_ROOT="$WORKSPACE_ROOT" AGENT_NAME="$AGENT_NAME" python3 - <<'PY' 2>/dev/null || echo ""
import os, sys
sys.path.insert(0, os.environ['REGISTRY_LIB'])
import registry
cfg = registry.load_registry(os.environ['WORKSPACE_ROOT']).legacy_view()
self_name = os.environ['AGENT_NAME']
for name, info in cfg.get('agents', {}).items():
    if name != self_name:
        model = info.get('model', 'sonnet')
        print(f'- **{name.title()}** ({model})')
PY
)
fi

# Copy the template unsubstituted: composition (lib/prompt_compose.py) resolves
# {{AGENT_NAME}}, {{CHANNELS}} and the rest at every spawn.
cp "$TEMPLATE_PATH" "$AGENT_DIR/SYSTEM_PROMPT.md"

# Create empty voice.md for user customization
touch "$AGENT_DIR/persona/voice.md"

# First-boot onboarding is for the primary only; builders, reviewers, the
# monitor and custom agents get none.
ONBOARDING_TEMPLATE="$WORKSPACE_ROOT/agents/templates/onboarding.md"
if [[ "$TEMPLATE" == "primary" && -f "$ONBOARDING_TEMPLATE" ]]; then
    cp "$ONBOARDING_TEMPLATE" "$AGENT_DIR/onboarding.md"
fi

# Create inbox directory for dispatch adapter
mkdir -p "$WORKSPACE_ROOT/inbox/$AGENT_NAME"

echo "  Created: $AGENT_DIR/"
echo "  System prompt generated from $TEMPLATE template"

# Register in agents.yaml (unless ephemeral). write_agent appends/edits via
# lib/registry.py, validates the result, and preserves existing comments.
if [[ "$EPHEMERAL" == "false" ]]; then
    REGISTRY_LIB="$REGISTRY_LIB" \
    WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    AGENT_NAME="$AGENT_NAME" \
    TEMPLATE="$TEMPLATE" \
    MODEL="$MODEL" \
    MAX_TURNS="$MAX_TURNS" \
    DISCORD_TOKEN="$DISCORD_TOKEN" \
    python3 - <<'PY'
import os
import sys

sys.path.insert(0, os.environ['REGISTRY_LIB'])
import registry

agent_name = os.environ['AGENT_NAME']
template = os.environ['TEMPLATE']
role = template if template in ('builder', 'reviewer') else 'custom'
entry = {
    'name': agent_name,
    'role': role,
    'model': os.environ['MODEL'],
    'max_turns': int(os.environ['MAX_TURNS']),
    'system_prompt': f'agents/{agent_name}/SYSTEM_PROMPT.md',
}

if os.environ.get('DISCORD_TOKEN', ''):
    # Note: user must add the actual token to .env
    entry['discord'] = {
        'token_env': 'DISCORD_BOT_TOKEN_' + agent_name.upper().replace('-', '_')
    }

try:
    registry.write_agent(os.environ['WORKSPACE_ROOT'], agent_name, entry)
except registry.RegistryError as e:
    print(e, file=sys.stderr)
    sys.exit(1)
PY
    echo "  Registered in agents.yaml"
fi

# Notify agent server (hot-load)
if curl -sf "$AGENT_SERVER/health" > /dev/null 2>&1; then
    RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
        "$AGENT_SERVER/agents/$AGENT_NAME/register" \
        ${AGENT_SERVER_TOKEN:+-H "Authorization: Bearer $AGENT_SERVER_TOKEN"} \
        -H "Content-Type: application/json" \
        -d '{}' 2>/dev/null)
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    if [[ "$HTTP_CODE" == "200" ]]; then
        echo "  Hot-registered with agent server"
    else
        echo "  Warning: failed to hot-register (HTTP $HTTP_CODE)" >&2
        echo "  Agent will be available after server restart" >&2
    fi
else
    echo "  Agent server not reachable — agent will be available after restart"
fi

echo ""
echo "Agent '$AGENT_NAME' created successfully."
echo "Customize: $AGENT_DIR/persona/voice.md"
