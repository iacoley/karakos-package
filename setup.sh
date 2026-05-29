#!/usr/bin/env bash
# Karakos Setup Wizard — Interactive installation and configuration

set -euo pipefail

# On any error, pause so the user can read the message before the window closes
pause_on_exit() {
    local rc=$?
    if [ $rc -ne 0 ]; then
        echo
        echo -e "\033[0;31mSetup failed (exit code $rc). Press any key to close.\033[0m"
        read -n 1 -s -r < /dev/tty 2>/dev/null || true
    fi
}
trap pause_on_exit EXIT

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${SCRIPT_DIR}/.setup-state.json"
ENV_FILE="${SCRIPT_DIR}/config/.env"
AGENTS_CONFIG="${SCRIPT_DIR}/config/agents.json"
CHANNELS_CONFIG="${SCRIPT_DIR}/config/channels.json"
DOCKER_COMPOSE="${SCRIPT_DIR}/config/docker-compose.yml"
KARAKOS_CONFIG="${SCRIPT_DIR}/.karakos/config.json"

# State management
load_state() {
    if [ -f "$STATE_FILE" ]; then
        cat "$STATE_FILE"
    else
        echo '{}'
    fi
}

save_state() {
    local key="$1"
    local value="$2"
    local state=$(load_state)
    echo "$state" | jq --arg k "$key" --arg v "$value" '.[$k] = $v' > "$STATE_FILE"
}

get_state() {
    local key="$1"
    load_state | jq -r ".${key} // empty"
}

# Logging
log() {
    echo -e "${GREEN}==>${NC} $*"
}

warn() {
    echo -e "${YELLOW}Warning:${NC} $*"
}

error() {
    echo -e "${RED}Error:${NC} $*" >&2
}

# Prerequisites check
check_prerequisites() {
    log "Checking prerequisites..."

    # Docker
    if ! command -v docker &> /dev/null; then
        error "Docker not found. Please install Docker:"
        error "  https://docs.docker.com/engine/install/"
        exit 1
    fi

    # Docker Compose
    if ! docker compose version &> /dev/null; then
        error "Docker Compose not found or wrong version."
        error "Please install Docker Compose v2:"
        error "  https://docs.docker.com/compose/install/"
        exit 1
    fi

    # Node.js / npm (for Claude CLI)
    if ! command -v npm &> /dev/null; then
        log "Node.js/npm not found — installing..."
        if command -v apt-get &> /dev/null; then
            # Debian/Ubuntu — use NodeSource LTS
            curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
            sudo apt-get install -y nodejs
        elif command -v brew &> /dev/null; then
            brew install node
        elif command -v dnf &> /dev/null; then
            sudo dnf install -y nodejs npm
        elif command -v pacman &> /dev/null; then
            sudo pacman -S --noconfirm nodejs npm
        else
            error "Could not auto-install Node.js. Install manually:"
            error "  https://nodejs.org/en/download"
            exit 1
        fi

        if ! command -v npm &> /dev/null; then
            error "Node.js installation failed. Install manually:"
            error "  https://nodejs.org/en/download"
            exit 1
        fi
        log "Node.js $(node --version) installed"
    fi

    # jq
    if ! command -v jq &> /dev/null; then
        log "jq not found — installing..."
        if command -v apt-get &> /dev/null; then
            sudo apt-get install -y jq
        elif command -v brew &> /dev/null; then
            brew install jq
        elif command -v dnf &> /dev/null; then
            sudo dnf install -y jq
        elif command -v pacman &> /dev/null; then
            sudo pacman -S --noconfirm jq
        else
            error "Could not auto-install jq. Install manually:"
            error "  https://stedolan.github.io/jq/download/"
            exit 1
        fi

        if ! command -v jq &> /dev/null; then
            error "jq installation failed."
            exit 1
        fi
        log "jq installed"
    fi

    # Check ports
    if lsof -Pi :3000 -sTCP:LISTEN -t >/dev/null 2>&1; then
        warn "Port 3000 already in use. Dashboard won't start."
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi

    if lsof -Pi :18791 -sTCP:LISTEN -t >/dev/null 2>&1; then
        error "Port 18791 already in use. Cannot continue."
        exit 1
    fi

    log "Prerequisites OK"
}

# Generate random token
generate_token() {
    local prefix="${1:-token}"
    echo "${prefix}_$(openssl rand -hex 32)"
}

# Prompt for input
prompt() {
    local prompt_text="$1"
    local var_name="$2"
    local default="${3:-}"

    if [ -n "$default" ]; then
        prompt_text="$prompt_text [$default]"
    fi

    read -p "$(echo -e ${BLUE}${prompt_text}:${NC} )" value < /dev/tty

    if [ -z "$value" ] && [ -n "$default" ]; then
        value="$default"
    fi

    printf -v "$var_name" '%s' "$value"
}

# Authenticate with Anthropic via Claude CLI
authenticate_claude() {
    log "Authenticating with Anthropic..."

    # Check if Claude CLI is installed
    if ! command -v claude &> /dev/null; then
        log "Installing Claude Code CLI..."
        npm install -g @anthropic-ai/claude-code
        if ! command -v claude &> /dev/null; then
            error "Failed to install Claude Code CLI."
            error "Install manually: npm install -g @anthropic-ai/claude-code"
            exit 1
        fi
    fi

    # Check if already authenticated by running a quick version check
    # (claude login status isn't directly queryable, so we just proceed)

    echo "This will open your browser to sign in with your Anthropic account."
    echo "No API key needed — just log in."
    echo

    claude login < /dev/tty

    if [ $? -eq 0 ]; then
        log "Authenticated successfully"
        return 0
    else
        error "Authentication failed. Run 'claude login' manually to retry."
        return 1
    fi
}

# Main setup flow
main() {
    echo "================================"
    echo "  Karakos Setup Wizard"
    echo "================================"
    echo

    # Check if resuming
    if [ -f "$STATE_FILE" ]; then
        log "Found previous setup state"
        read -p "Resume from previous setup? (Y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Nn]$ ]]; then
            rm "$STATE_FILE"
            log "Starting fresh"
        fi
    fi

    check_prerequisites

    # Step 1: System name
    if [ -z "$(get_state system_name)" ]; then
        echo
        log "Step 1: System Name"
        prompt "What would you like to name your system" SYSTEM_NAME
        save_state system_name "$SYSTEM_NAME"
    else
        SYSTEM_NAME=$(get_state system_name)
        log "System name: $SYSTEM_NAME"
    fi

    # Step 2: Owner name
    if [ -z "$(get_state owner_name)" ]; then
        echo
        log "Step 2: Owner Name"
        prompt "Your name (for addressing you)" OWNER_NAME
        save_state owner_name "$OWNER_NAME"
    else
        OWNER_NAME=$(get_state owner_name)
        log "Owner: $OWNER_NAME"
    fi

    # Step 3: Primary agent name
    if [ -z "$(get_state primary_agent_name)" ]; then
        echo
        log "Step 3: Primary Agent Name"
        prompt "Name for your primary agent" PRIMARY_AGENT_NAME "$SYSTEM_NAME"
        save_state primary_agent_name "$PRIMARY_AGENT_NAME"
    else
        PRIMARY_AGENT_NAME=$(get_state primary_agent_name)
        log "Primary agent: $PRIMARY_AGENT_NAME"
    fi

    # Step 4: Anthropic authentication
    echo
    log "Step 4: Anthropic Login"
    authenticate_claude

    # Step 4b: Local model backend (optional)
    echo
    log "Step 4b: Local Model Backend (optional)"
    AGENT_BACKEND="claude"
    OLLAMA_HOST="http://host.docker.internal:11434"
    OLLAMA_MODEL="llama3.1"
    echo "Karakos can run the builder, reviewer, session summarizer, and memory"
    echo "importance scorer against a local Ollama server instead of Claude."
    echo "(The always-on primary/relay agents still use Claude.)"
    read -p "Use a local Ollama server for those tasks? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        AGENT_BACKEND="ollama"
        prompt "Ollama server URL" OLLAMA_HOST "$OLLAMA_HOST"
        prompt "Ollama model tag" OLLAMA_MODEL "$OLLAMA_MODEL"
        log "Local backend enabled (model: $OLLAMA_MODEL). Ensure 'ollama serve' is running and the model is pulled."
    fi

    # Step 5: Discord setup
    if [ -z "$(get_state discord_bot_token)" ]; then
        echo
        log "Step 5: Discord Bot Setup"
        echo "You need to create a Discord bot application."
        echo "Follow the guide in docs/DISCORD_SETUP.md"
        echo "Required: Bot token and bot user ID"
        echo

        prompt "Discord bot token" DISCORD_BOT_TOKEN
        prompt "Discord bot user ID" DISCORD_BOT_ID
        prompt "Discord server ID" DISCORD_SERVER_ID

        save_state discord_bot_token "$DISCORD_BOT_TOKEN"
        save_state discord_bot_id "$DISCORD_BOT_ID"
        save_state discord_server_id "$DISCORD_SERVER_ID"
    else
        DISCORD_BOT_TOKEN=$(get_state discord_bot_token)
        DISCORD_BOT_ID=$(get_state discord_bot_id)
        DISCORD_SERVER_ID=$(get_state discord_server_id)
        log "Discord bot configured"
    fi

    # Step 6: Discord channels
    if [ -z "$(get_state channel_general)" ]; then
        echo
        log "Step 6: Discord Channel IDs"
        echo "Right-click channels in Discord → Copy Channel ID"
        echo

        prompt "General channel ID" CHANNEL_GENERAL
        prompt "Signals channel ID" CHANNEL_SIGNALS
        prompt "Staff-comms channel ID (optional)" CHANNEL_STAFF ""

        save_state channel_general "$CHANNEL_GENERAL"
        save_state channel_signals "$CHANNEL_SIGNALS"
        save_state channel_staff "$CHANNEL_STAFF"
    else
        CHANNEL_GENERAL=$(get_state channel_general)
        CHANNEL_SIGNALS=$(get_state channel_signals)
        CHANNEL_STAFF=$(get_state channel_staff)
        log "Channels configured"
    fi

    # Step 7: Owner Discord ID
    if [ -z "$(get_state owner_discord_id)" ]; then
        echo
        log "Step 7: Your Discord User ID"
        echo "Right-click your username in Discord → Copy User ID"
        prompt "Your Discord user ID" OWNER_DISCORD_ID

        save_state owner_discord_id "$OWNER_DISCORD_ID"
    else
        OWNER_DISCORD_ID=$(get_state owner_discord_id)
        log "Owner Discord ID: $OWNER_DISCORD_ID"
    fi

    # Step 8: Cost limits
    if [ -z "$(get_state cost_daily_limit)" ]; then
        echo
        log "Step 8: Cost Limits"
        echo "Typical usage: \$5-15/week"
        prompt "Daily spend limit (USD)" COST_DAILY_LIMIT "25.00"
        prompt "Monthly spend limit (USD)" COST_MONTHLY_LIMIT "500.00"

        save_state cost_daily_limit "$COST_DAILY_LIMIT"
        save_state cost_monthly_limit "$COST_MONTHLY_LIMIT"
    else
        COST_DAILY_LIMIT=$(get_state cost_daily_limit)
        COST_MONTHLY_LIMIT=$(get_state cost_monthly_limit)
        log "Cost limits: \$$COST_DAILY_LIMIT/day, \$$COST_MONTHLY_LIMIT/month"
    fi

    # Generate tokens
    AGENT_SERVER_TOKEN=$(generate_token "krkos")
    SESSION_SECRET=$(openssl rand -hex 32)
    DASHBOARD_PASSWORD=$(openssl rand -base64 16)

    # Create .env file
    log "Generating configuration files..."

    mkdir -p config
    chmod 700 config

    cat > "$ENV_FILE" <<EOF
# Karakos System Configuration
# Generated by setup.sh — NEVER commit this file to git
# File permissions: 600 (owner read/write only)
# Anthropic auth handled by 'claude login' — no API key needed

# Model backend for builder/reviewer/summarizer/memory scoring
# ("claude" or "ollama"; primary/relay always use Claude)
AGENT_BACKEND=$AGENT_BACKEND
OLLAMA_HOST=$OLLAMA_HOST
OLLAMA_MODEL=$OLLAMA_MODEL

# Agent server authentication
AGENT_SERVER_TOKEN=$AGENT_SERVER_TOKEN

# Dashboard session signing (shared between auth and verification)
SESSION_SECRET=$SESSION_SECRET

# Dashboard authentication
DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD

# Discord — primary agent bot
DISCORD_BOT_TOKEN_PRIMARY=$DISCORD_BOT_TOKEN
DISCORD_BOT_ID_PRIMARY=$DISCORD_BOT_ID
DISCORD_SERVER_ID=$DISCORD_SERVER_ID

# Discord channels
DISCORD_CHANNEL_GENERAL=$CHANNEL_GENERAL
DISCORD_CHANNEL_SIGNALS=$CHANNEL_SIGNALS

# System identity
SYSTEM_NAME=$SYSTEM_NAME
OWNER_NAME=$OWNER_NAME
OWNER_DISCORD_ID=$OWNER_DISCORD_ID
WORKSPACE_ROOT=/workspace

# Network
DASHBOARD_PORT=3000
AGENT_SERVER_PORT=18791
TZ=UTC

# Cost limits
COST_DAILY_LIMIT=$COST_DAILY_LIMIT
COST_MONTHLY_LIMIT=$COST_MONTHLY_LIMIT

# Concurrency
MAX_CONCURRENT_BUILDERS=1
MAX_CONCURRENT_REVIEWERS=2

# Memory tuning
MEMORY_DECAY_RATE=0.25
MEMORY_CUTOFF=6.0
MEMORY_MAX_EPISODES=15

# Retention
MESSAGE_RETENTION_DAYS=90
EOF

    chmod 600 "$ENV_FILE"

    # Create agents.json
    cat > "$AGENTS_CONFIG" <<EOF
{
  "agents": {
    "${PRIMARY_AGENT_NAME}": {
      "model": "sonnet",
      "max_turns": 200,
      "timeout": 10800,
      "system_prompt": "agents/${PRIMARY_AGENT_NAME}/SYSTEM_PROMPT.md",
      "tool_streaming": true,
      "stream_to_channel": true,
      "discord_bot_token_env": "DISCORD_BOT_TOKEN_PRIMARY",
      "discord_bot_id_env": "DISCORD_BOT_ID_PRIMARY"
    },
    "relay": {
      "model": "haiku",
      "max_turns": 10,
      "timeout": 300,
      "system_prompt": "agents/relay/SYSTEM_PROMPT.md",
      "tool_streaming": false,
      "stream_to_channel": false
    }
  }
}
EOF

    # Create channels.json
    CHANNELS_JSON="{\"server_id\": \"$DISCORD_SERVER_ID\", \"channels\": {\"general\": {\"id\": \"$CHANNEL_GENERAL\", \"default_agent\": \"${PRIMARY_AGENT_NAME}\"}, \"signals\": {\"id\": \"$CHANNEL_SIGNALS\", \"default_agent\": null}"

    if [ -n "$CHANNEL_STAFF" ]; then
        CHANNELS_JSON="${CHANNELS_JSON}, \"staff-comms\": {\"id\": \"$CHANNEL_STAFF\", \"default_agent\": null}"
    fi

    CHANNELS_JSON="${CHANNELS_JSON}}}"

    echo "$CHANNELS_JSON" | jq '.' > "$CHANNELS_CONFIG"

    # Create .karakos/config.json
    mkdir -p .karakos
    cat > "$KARAKOS_CONFIG" <<EOF
{
  "version": "1.0.0",
  "system_name": "$SYSTEM_NAME",
  "owner_name": "$OWNER_NAME",
  "installed_at": "$(date -Iseconds)"
}
EOF

    # Generate agent directories and system prompts
    log "Creating agent directories..."

    for agent in "${PRIMARY_AGENT_NAME}" "relay"; do
        mkdir -p "agents/${agent}/persona"
        mkdir -p "agents/${agent}/inbox"
        mkdir -p "agents/${agent}/journal"

        # Generate system prompt from template
        if [ "$agent" = "${PRIMARY_AGENT_NAME}" ]; then
            template="agents/templates/primary.md"
        else
            template="agents/templates/${agent}.md"
        fi

        # Simple variable substitution
        sed "s/{{AGENT_NAME}}/${agent}/g; s/{{SYSTEM_NAME}}/${SYSTEM_NAME}/g; s/{{OWNER_NAME}}/${OWNER_NAME}/g" \
            "$template" > "agents/${agent}/SYSTEM_PROMPT.md"
    done

    # docker-compose.yml lives in config/ (shipped with the repo).
    # No generation needed — install.sh runs docker compose from config/.

    # Update .gitignore
    if ! grep -q "config/.env" .gitignore 2>/dev/null; then
        echo "config/.env" >> .gitignore
    fi

    log "Configuration complete!"
    echo
    echo "================================"
    echo "  Setup Complete"
    echo "================================"
    echo
    echo "Dashboard password: $DASHBOARD_PASSWORD"
    echo
    warn "Save this password! It's stored in config/.env"
    warn "NEVER commit config/.env to git!"
    echo

    # Clean up state file
    rm -f "$STATE_FILE"

    # Launch
    echo
    read -p "$(echo -e ${GREEN}Ready to launch ${SYSTEM_NAME}? Press Enter to start...${NC})" < /dev/tty
    echo
    log "Pulling karakos image from GHCR (this can take a few minutes on first install)..."
    if ! docker compose -f "$DOCKER_COMPOSE" --env-file "$ENV_FILE" pull; then
        echo
        error "Failed to pull the karakos image from GHCR."
        error ""
        error "Common causes:"
        error "  1. The package is not yet published (a new release.yml run has not"
        error "     completed) — check https://github.com/mcarmody/karakos-package/pkgs/container/karakos"
        error "  2. The GHCR package is set to private. Public visibility must be"
        error "     enabled at github.com/mcarmody/karakos-package/pkgs/container/karakos"
        error "     → Package settings → Change package visibility → Public"
        error "  3. Network or DNS issue reaching ghcr.io."
        error ""
        error "Once the image is pullable, run this to finish setup:"
        error "  docker compose -f $DOCKER_COMPOSE --env-file $ENV_FILE up -d"
        exit 1
    fi
    log "Starting services..."
    docker compose -f "$DOCKER_COMPOSE" --env-file "$ENV_FILE" up -d

    echo
    log "Services are starting up"
    echo "  Dashboard: http://localhost:3000 (login: admin / password above)"
    echo "  Check #signals in Discord for system startup"
    echo
}

# Handle --clean flag
if [ "${1:-}" = "--clean" ]; then
    log "Cleaning setup state and generated files..."
    rm -f "$STATE_FILE"
    rm -f "$ENV_FILE"
    rm -f "$AGENTS_CONFIG"
    rm -f "$CHANNELS_CONFIG"
    rm -f "$KARAKOS_CONFIG"
    log "Clean complete. Run ./setup.sh to start fresh."
    exit 0
fi

main
