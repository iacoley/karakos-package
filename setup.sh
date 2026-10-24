#!/usr/bin/env bash
# Karakos Setup Wizard — Interactive installation and configuration
#
#   ./setup.sh                       interactive wizard
#   ./setup.sh --answers FILE.json   unattended: same fields and checks, no prompts
#                                    (KARAKOS_ANSWERS=FILE.json does the same;
#                                    see docs/answers.example.json)
#   ./setup.sh --native              no Docker: same wizard, then bin/native-install.sh
#                                    (KARAKOS_NATIVE=1 does the same; see docs/NATIVE.md)
#   ./setup.sh --clean               remove generated state and config

set -euo pipefail

# On any error, pause so the user can read the message before the window closes
pause_on_exit() {
    local rc=$?
    if [ $rc -ne 0 ]; then
        echo
        if [ -n "${ANSWERS_FILE:-}" ]; then
            echo -e "\033[0;31mSetup failed (exit code $rc).\033[0m" >&2
        else
            echo -e "\033[0;31mSetup failed (exit code $rc). Press any key to close.\033[0m"
            read -n 1 -s -r < /dev/tty 2>/dev/null || true
        fi
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
AGENTS_CONFIG="${SCRIPT_DIR}/config/agents.yaml"
CHANNELS_CONFIG="${SCRIPT_DIR}/config/channels.json"
DOCKER_COMPOSE="${SCRIPT_DIR}/config/docker-compose.yml"
KARAKOS_CONFIG="${SCRIPT_DIR}/.karakos/config.json"
ANSWERS_FILE="${KARAKOS_ANSWERS:-}"

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

    if [ "$NATIVE" != 1 ]; then
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

    # python3 + PyYAML: setup writes config/agents.yaml through lib/registry.py.
    if ! command -v python3 &> /dev/null; then
        error "python3 not found. Install Python 3.9 or newer and re-run setup."
        exit 1
    fi
    if ! python3 -c 'import yaml' &> /dev/null; then
        log "PyYAML not found — installing..."
        if command -v apt-get &> /dev/null; then
            sudo apt-get install -y python3-yaml
        elif command -v brew &> /dev/null; then
            python3 -m pip install --user pyyaml
        elif command -v dnf &> /dev/null; then
            sudo dnf install -y python3-pyyaml
        elif command -v pacman &> /dev/null; then
            sudo pacman -S --noconfirm python-yaml
        else
            python3 -m pip install --user pyyaml || true
        fi

        if ! python3 -c 'import yaml' &> /dev/null; then
            error "PyYAML installation failed. Install it (python3 -m pip install pyyaml) and re-run setup."
            exit 1
        fi
        log "PyYAML installed"
    fi

    # Check ports
    if lsof -Pi :3000 -sTCP:LISTEN -t >/dev/null 2>&1; then
        warn "Port 3000 already in use. Dashboard won't start."
        if [ -z "$ANSWERS_FILE" ]; then
            read -p "Continue anyway? (y/N) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
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

# Unattended mode: validate the answers file, then load every answer into the
# wizard's state file so each step below finds its value and skips its prompt.
# lib/setup_answers.py owns the fields, defaults and checks.
load_answers() {
    local parsed
    [ -f "$ANSWERS_FILE" ] || { error "answers file not found: $ANSWERS_FILE"; exit 1; }
    parsed=$(python3 "${SCRIPT_DIR}/lib/setup_answers.py" "$ANSWERS_FILE") || exit 1
    rm -f "$STATE_FILE"
    (umask 077; echo "$parsed" | jq '{
        system_name, owner_name, primary_agent_name, monitor_agent_name,
        discord_bot_token, discord_bot_id, discord_server_id,
        channel_general, channel_signals, channel_staff, owner_discord_id,
        cost_daily_limit, cost_monthly_limit, claude_oauth_token}' > "$STATE_FILE")
    log "Loaded answers from $ANSWERS_FILE"
}

# Unattended Anthropic auth: there is no browser. Use the token from the answers
# file (written to config/.env later) or a login already present on this host.
authenticate_claude_unattended() {
    CLAUDE_OAUTH_TOKEN=$(get_state claude_oauth_token)
    if [ -n "$CLAUDE_OAUTH_TOKEN" ]; then
        log "Using claude_oauth_token from the answers file"
        # docker-compose.yml bind-mounts these; a missing file would become a directory
        mkdir -p "$HOME/.claude"
        [ -e "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"
    elif [ -d "$HOME/.claude" ] && [ -e "$HOME/.claude.json" ]; then
        log "Using the existing claude login in $HOME/.claude"
    else
        error "No Anthropic credentials. Run 'claude login' on this host first, or set"
        error "claude_oauth_token (or claude_oauth_token_env) in the answers file."
        exit 1
    fi
}

# Main setup flow
main() {
    echo "================================"
    echo "  Karakos Setup Wizard"
    echo "================================"
    echo

    if [ -n "$ANSWERS_FILE" ]; then
        load_answers
    fi

    # Check if resuming
    if [ -z "$ANSWERS_FILE" ] && [ -f "$STATE_FILE" ]; then
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
    # Display name stays as typed (trimmed); the registry id is a slug of it
    # (lowercase letters, digits, hyphens). lib/registry.py owns both rules.
    PRIMARY_AGENT_NAME=$(printf '%s' "$PRIMARY_AGENT_NAME" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    PRIMARY_AGENT_ID=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import registry; print(registry.slugify_id(sys.argv[2]))' "${SCRIPT_DIR}/lib" "$PRIMARY_AGENT_NAME")

    # Monitoring agent name. The id is the same slug rule as the primary's, and must
    # not name a system component (lib/registry.py refuses relay, scheduler,
    # mcp-tools, server) or collide with the primary.
    while :; do
        if [ -z "$(get_state monitor_agent_name)" ]; then
            prompt "Name for your monitoring agent" MONITOR_AGENT_NAME "monitor"
            save_state monitor_agent_name "$MONITOR_AGENT_NAME"
        else
            MONITOR_AGENT_NAME=$(get_state monitor_agent_name)
        fi
        MONITOR_AGENT_NAME=$(printf '%s' "$MONITOR_AGENT_NAME" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
        MONITOR_AGENT_ID=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import registry; print(registry.slugify_id(sys.argv[2]))' "${SCRIPT_DIR}/lib" "$MONITOR_AGENT_NAME")
        case "$MONITOR_AGENT_ID" in
            relay|scheduler|mcp-tools|server|"$PRIMARY_AGENT_ID")
                error "'$MONITOR_AGENT_NAME' cannot be the monitoring agent's name (reserved, or the same as the primary); choose another"
                save_state monitor_agent_name ""
                ;;
            *) break ;;
        esac
    done

    # Step 4: Anthropic authentication
    echo
    log "Step 4: Anthropic Login"
    if [ -n "$ANSWERS_FILE" ]; then
        authenticate_claude_unattended
    else
        authenticate_claude
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
    # Native installs resolve the root from the checkout (lib/paths); a /workspace
    # here would override it for everything that sources this file.
    if [ "${NATIVE:-0}" = "1" ]; then
        sed -i '/^WORKSPACE_ROOT=/d' "$ENV_FILE"
    fi
    if [ -n "${CLAUDE_OAUTH_TOKEN:-}" ]; then
        printf '\n# Anthropic auth from the answers file (no browser login)\nCLAUDE_CODE_OAUTH_TOKEN=%s\n' \
            "$CLAUDE_OAUTH_TOKEN" >> "$ENV_FILE"
    fi

    chmod 600 "$ENV_FILE"

    # Create agents.yaml (schema 2) through the registry, so quoting is the YAML
    # library's problem, not the shell's. Refuses an existing file.
    if [ -f "$AGENTS_CONFIG" ]; then
        warn "config/agents.yaml already exists; leaving it untouched"
    else
        python3 "${SCRIPT_DIR}/lib/registry.py" init --workspace "${SCRIPT_DIR}" \
            --primary-id "${PRIMARY_AGENT_ID}" --primary-name "${PRIMARY_AGENT_NAME}" \
            --monitor-id "${MONITOR_AGENT_ID}" --monitor-name "${MONITOR_AGENT_NAME}" \
            --monitor-template agents/templates/monitor.md \
            --channel general
    fi

    # Build queue database (spec 3.3): same schema function the migrator uses.
    # The queue stays off until config/build-queue.yaml says `enabled: true`.
    python3 "${SCRIPT_DIR}/lib/buildq.py" init --db "${SCRIPT_DIR}/data/build-queue.db" \
        || warn "could not create data/build-queue.db (run karakos migrate)"

    # Create channels.json
    CHANNELS_JSON="{\"server_id\": \"$DISCORD_SERVER_ID\", \"channels\": {\"general\": {\"id\": \"$CHANNEL_GENERAL\"}, \"signals\": {\"id\": \"$CHANNEL_SIGNALS\"}"

    if [ -n "$CHANNEL_STAFF" ]; then
        CHANNELS_JSON="${CHANNELS_JSON}, \"staff-comms\": {\"id\": \"$CHANNEL_STAFF\"}"
    fi

    CHANNELS_JSON="${CHANNELS_JSON}}}"

    echo "$CHANNELS_JSON" | jq '.' > "$CHANNELS_CONFIG"

    # Create .karakos/config.json
    mkdir -p .karakos
    jq -n --arg system_name "$SYSTEM_NAME" --arg owner_name "$OWNER_NAME" \
        --arg installed_at "$(date -Iseconds)" \
        '{version: "1.0.0", system_name: $system_name, owner_name: $owner_name, installed_at: $installed_at}' \
        > "$KARAKOS_CONFIG"

    # Generate agent directories and system prompts
    log "Creating agent directories..."

    for agent in "${PRIMARY_AGENT_ID}" "${MONITOR_AGENT_ID}"; do
        mkdir -p "agents/${agent}/persona"
        mkdir -p "agents/${agent}/inbox"
        mkdir -p "agents/${agent}/journal"

        # Copy the template unsubstituted: placeholders are resolved at every
        # spawn from the registry and channels.json (lib/prompt_compose.py).
        if [ "$agent" = "${PRIMARY_AGENT_ID}" ]; then
            template="agents/templates/primary.md"
            # First-boot onboarding: the primary only.
            cp "agents/templates/onboarding.md" "agents/${agent}/onboarding.md"
        else
            template="agents/templates/monitor.md"
        fi
        cp "$template" "agents/${agent}/SYSTEM_PROMPT.md"
    done

    # docker-compose.yml lives in config/ (shipped with the repo).
    # No generation needed — install.sh runs docker compose from config/.

    # Generate the hooks section of config/claude-settings.json from config/hooks.json.
    python3 "${SCRIPT_DIR}/bin/hooks-sync.py" "${SCRIPT_DIR}" || warn "hooks-sync failed; safety hooks not wired"

    # Fresh install only: create the graph memory schema (and stamp the empty
    # data dir first). An existing data dir is left alone; the migrator owns it.
    if [ ! -e "${SCRIPT_DIR}/data" ] || [ -z "$(ls -A "${SCRIPT_DIR}/data" 2>/dev/null)" ]; then
        (cd "${SCRIPT_DIR}" && python3 -m lib.graph init "${SCRIPT_DIR}/data") \
            || warn "graph init failed; run: python3 -m lib.graph init"
    fi

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
    if [ -z "$ANSWERS_FILE" ]; then
        read -p "$(echo -e ${GREEN}Ready to launch ${SYSTEM_NAME}? Press Enter to start...${NC})" < /dev/tty
    fi
    echo
    if [ "$NATIVE" = 1 ]; then
        log "Native install (no Docker): venv, dashboard, systemd user units..."
        bash "$SCRIPT_DIR/bin/native-install.sh"
        echo
        echo "  Dashboard: http://localhost:${DASHBOARD_PORT:-3000} (login: admin / password above)"
        echo "  Logs:      journalctl --user -u 'karakos-*' -f"
        return 0
    fi
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

# --native: no Docker. Same wizard and config/, then bin/native-install.sh.
NATIVE=0
[ "${KARAKOS_NATIVE:-}" = "1" ] && NATIVE=1
_args=()
for _a in "$@"; do
    if [ "$_a" = "--native" ]; then NATIVE=1; else _args+=("$_a"); fi
done
set -- ${_args[@]+"${_args[@]}"}

# Handle --answers FILE / --answers=FILE (KARAKOS_ANSWERS is the env spelling)
case "${1:-}" in
    --answers)
        [ -n "${2:-}" ] || { error "--answers needs a file"; exit 2; }
        ANSWERS_FILE="$2" ;;
    --answers=*) ANSWERS_FILE="${1#--answers=}" ;;
esac
if [ -n "$ANSWERS_FILE" ]; then
    case "$ANSWERS_FILE" in /*) ;; *) ANSWERS_FILE="$PWD/$ANSWERS_FILE" ;; esac
fi

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
