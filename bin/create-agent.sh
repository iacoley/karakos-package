#!/usr/bin/env bash
# create-agent.sh — Create a new agent at runtime
#
# Usage:
#   create-agent.sh --template primary --model sonnet oracle
#   create-agent.sh --template builder --ephemeral temp-builder

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"
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
            echo "  --template NAME        Base template: primary, relay, builder, reviewer (default: primary)"
            echo "  --model MODEL          Claude model: opus, sonnet, haiku (default: sonnet)"
            echo "  --discord-token TOKEN  Discord bot token (optional)"
            echo "  --ephemeral            Don't persist to agents.json"
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

# Check template exists
TEMPLATE_PATH="$WORKSPACE_ROOT/agents/templates/$TEMPLATE.md"
if [[ ! -f "$TEMPLATE_PATH" ]]; then
    echo "Error: template not found: $TEMPLATE_PATH" >&2
    echo "Available templates: $(ls "$WORKSPACE_ROOT/agents/templates/" | sed 's/.md$//' | tr '\n' ' ')" >&2
    exit 1
fi

# Check for name conflict
AGENTS_JSON="$WORKSPACE_ROOT/config/agents.json"
if [[ -f "$AGENTS_JSON" ]]; then
    EXISTING=$(AGENTS_JSON="$AGENTS_JSON" python3 - <<'PY' 2>/dev/null || echo ""
import json, os
cfg = json.load(open(os.environ['AGENTS_JSON']))
print(' '.join(cfg.get('agents', {}).keys()))
PY
)
    if echo "$EXISTING" | grep -qw "$AGENT_NAME"; then
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
    CHANNELS=$(CHANNELS_JSON="$CHANNELS_JSON" python3 - <<'PY' 2>/dev/null || echo "- #general"
import json, os
cfg = json.load(open(os.environ['CHANNELS_JSON']))
for name, info in cfg.get('channels', {}).items():
    default = info.get('default_agent', '')
    print(f'- #{name}' + (f' (default: {default})' if default else ''))
PY
)
fi

# Build other agents list
OTHER_AGENTS=""
if [[ -f "$AGENTS_JSON" ]]; then
    OTHER_AGENTS=$(AGENTS_JSON="$AGENTS_JSON" AGENT_NAME="$AGENT_NAME" python3 - <<'PY' 2>/dev/null || echo ""
import json, os
cfg = json.load(open(os.environ['AGENTS_JSON']))
self_name = os.environ['AGENT_NAME']
for name, info in cfg.get('agents', {}).items():
    if name != self_name:
        model = info.get('model', 'sonnet')
        print(f'- **{name.title()}** ({model})')
PY
)
fi

# Generate system prompt from template
# sed can't safely substitute multi-line replacement text (CHANNELS/
# OTHER_AGENTS routinely span several lines once more than one channel
# or agent exists) -- "unterminated `s' command" the moment a newline
# lands in the replacement. python handles it directly instead.
AGENT_NAME="$AGENT_NAME" SYSTEM_NAME="$SYSTEM_NAME" OWNER_NAME="$OWNER_NAME" \
CHANNELS="$CHANNELS" OTHER_AGENTS="$OTHER_AGENTS" \
TEMPLATE_PATH="$TEMPLATE_PATH" OUT_PATH="$AGENT_DIR/SYSTEM_PROMPT.md" \
python3 - <<'PY'
import os
text = open(os.environ["TEMPLATE_PATH"]).read()
for key in ("AGENT_NAME", "SYSTEM_NAME", "OWNER_NAME", "CHANNELS", "OTHER_AGENTS"):
    text = text.replace("{{" + key + "}}", os.environ[key])
open(os.environ["OUT_PATH"], "w").write(text)
PY

# Create empty voice.md for user customization
touch "$AGENT_DIR/persona/voice.md"

# Drop in a templated onboarding.md so the very first turn asks the user
# for guidance instead of behaving as a fully-formed assistant. Removed
# automatically (well — ignored) once the persona dir has any content.
ONBOARDING_TEMPLATE="$WORKSPACE_ROOT/agents/templates/onboarding.md"
if [[ -f "$ONBOARDING_TEMPLATE" ]]; then
    sed \
        -e "s/{{AGENT_NAME}}/$AGENT_NAME/g" \
        -e "s/{{SYSTEM_NAME}}/$SYSTEM_NAME/g" \
        -e "s/{{OWNER_NAME}}/$OWNER_NAME/g" \
        "$ONBOARDING_TEMPLATE" > "$AGENT_DIR/onboarding.md"
fi

# Create inbox directory for dispatch adapter
mkdir -p "$WORKSPACE_ROOT/inbox/$AGENT_NAME"

echo "  Created: $AGENT_DIR/"
echo "  System prompt generated from $TEMPLATE template"

# Register in agents.json (unless ephemeral)
if [[ "$EPHEMERAL" == "false" && -f "$AGENTS_JSON" ]]; then
    AGENTS_JSON="$AGENTS_JSON" \
    AGENT_NAME="$AGENT_NAME" \
    MODEL="$MODEL" \
    MAX_TURNS="$MAX_TURNS" \
    DISCORD_TOKEN="$DISCORD_TOKEN" \
    python3 - <<'PY'
import json
import os

agents_json = os.environ['AGENTS_JSON']
agent_name = os.environ['AGENT_NAME']
model = os.environ['MODEL']
max_turns = int(os.environ['MAX_TURNS'])
discord_token = os.environ.get('DISCORD_TOKEN', '')

with open(agents_json) as f:
    cfg = json.load(f)

agents = cfg.setdefault('agents', {})
entry = {
    'model': model,
    'max_turns': max_turns,
    'system_prompt': f'agents/{agent_name}/SYSTEM_PROMPT.md',
}

if discord_token:
    env_var = 'DISCORD_BOT_TOKEN_' + agent_name.upper().replace('-', '_')
    entry['discord_bot_token_env'] = env_var
    # Note: user must add the actual token to .env

agents[agent_name] = entry

with open(agents_json, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
PY
    echo "  Registered in agents.json"
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
