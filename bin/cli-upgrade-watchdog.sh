#!/usr/bin/env bash
# cli-upgrade-watchdog.sh — catch a Claude CLI upgrade that arrived without us.
#
# upgrade-claude-cli.sh guards the upgrades it performs. This guards the ones
# it does not: `docker compose pull` onto a rebuilt image, an operator's
# `npm install -g`, anything that swaps the CLI out from under a running
# install. That is the path a bad upstream release actually travels, so a
# rollback that only covers our own updater covers the rarer half.
#
# Three properties this turns on:
#
# **It only spends a turn when the version moved.** Re-verifying on every tick
# would burn an API call an hour and would page for API outages instead of bad
# releases. Drift from the recorded known-good is the trigger.
#
# **It reverts to a version it watched work, not to "previous".** The known-good
# record is only written after a probe turn came back, so the rollback target
# is a version this install has actually answered a message on.
#
# **The notice goes direct to Discord, never through poke.sh.** poke.sh queues
# a message FOR AN AGENT, and the situation here is precisely that agents
# cannot complete a turn — the alert would land in the queue nobody can read.
# Same reasoning as bin/wedge-check.py.
#
# First run adopts whatever is installed as known-good without verifying: at
# that point there is nothing to roll back to, so a failed probe would have no
# action attached to it, and paying for an API turn to learn nothing actionable
# on every fresh install is a bad trade.
#
# Exit codes:
#   0  no drift, or the new version verified and was adopted
#   1  the new version failed; it was reverted and a notice was posted
#   2  the new version failed and the revert failed — this install may be dark
#   3  could not run (no CLI, unreadable version)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"

# Overridable only so the tests can watch the real call happen against a
# recording stand-in; the command name and arguments stay production ones.
NOTIFY_BIN_DIR="${KARAKOS_NOTIFY_BIN_DIR:-$SCRIPT_DIR}"
UPGRADE_CLI="$SCRIPT_DIR/upgrade-claude-cli.sh"

STATE_FILE="${CLAUDE_CLI_STATE_FILE:-$WORKSPACE_ROOT/data/health/claude-cli.json}"
ALERT_CHANNEL="${CLI_ALERT_CHANNEL:-signals}"
NPM_PACKAGE="${CLAUDE_CLI_PACKAGE:-@anthropic-ai/claude-code}"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] cli-watchdog: $*"; }

usage() {
    sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

read_known_good() {
    [[ -f "$STATE_FILE" ]] || return 0
    python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if isinstance(data, dict) and isinstance(data.get("known_good"), str):
    print(data["known_good"])
' "$STATE_FILE" 2>/dev/null || true
}

write_known_good() {
    local version="$1"
    mkdir -p "$(dirname "$STATE_FILE")"
    python3 -c '
import json, sys, datetime
path, version = sys.argv[1], sys.argv[2]
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump({"known_good": version,
               "verified_at": datetime.datetime.now().isoformat()}, fh)
import os
os.replace(tmp, path)
' "$STATE_FILE" "$version"
}

# Direct to Discord with the bot token. NOT the agent queue — see the header.
notify() {
    local message="$1"
    local notifier="$NOTIFY_BIN_DIR/discord-notify.sh"
    if [[ ! -f "$notifier" ]]; then
        echo "cli-watchdog: no discord-notify.sh, notice not sent: $message" >&2
        return 1
    fi
    if ! bash "$notifier" "$ALERT_CHANNEL" "$message" >/dev/null 2>&1; then
        echo "cli-watchdog: notice failed to post: $message" >&2
        return 1
    fi
    log "posted notice to #$ALERT_CHANNEL"
}

run_watchdog() {
    local installed
    if ! installed=$(bash "$UPGRADE_CLI" --print-version 2>/dev/null); then
        log "no readable Claude CLI version — nothing to watch"
        return 3
    fi
    installed="${installed//[$'\n\r']/}"
    [[ -n "$installed" ]] || { log "no readable Claude CLI version"; return 3; }

    local known_good
    known_good=$(read_known_good)

    if [[ -z "$known_good" ]]; then
        write_known_good "$installed"
        log "adopted $installed as known-good (nothing to roll back to yet)"
        return 0
    fi

    if [[ "$installed" == "$known_good" ]]; then
        log "CLI unchanged at $installed"
        return 0
    fi

    log "CLI changed underneath us: known-good $known_good, installed $installed — probing"
    if bash "$UPGRADE_CLI" --verify-only; then
        write_known_good "$installed"
        log "$installed completed a turn; adopted as known-good"
        return 0
    fi

    log "$installed cannot complete a turn — reverting to $known_good"
    if ! bash "$UPGRADE_CLI" --install "$known_good"; then
        notify "🚨 Claude CLI \`$installed\` arrived on this install and cannot complete a turn, and the revert to \`$known_good\` **also failed**. Messages will go unanswered until the CLI is repaired by hand: \`npm install -g ${NPM_PACKAGE}@${known_good}\`." || true
        return 2
    fi

    if bash "$UPGRADE_CLI" --verify-only; then
        notify "⚠️ Claude CLI upgrade reverted. \`$installed\` shipped onto this install and could not complete a turn, so \`$known_good\` was reinstalled and now answers normally." || true
    else
        notify "⚠️ Claude CLI upgrade reverted (\`$installed\` → \`$known_good\`), but a turn still fails. The fault may not be the upgrade — check API credentials and connectivity." || true
    fi
    return 1
}

# --------------------------------------------------------------------------
# Selftest — a positive control for the drift path.
#
# Fakes npm, claude and discord-notify on PATH, records a known-good, swaps in
# a CLI version that cannot complete a turn, and fails unless the real script
# reverted it and posted a notice. Also runs the negative control: a version
# that works must be adopted, not rolled back.
# --------------------------------------------------------------------------
run_selftest() {
    if [[ -n "${KARAKOS_SELFTEST_DIR:-}" ]]; then
        echo "selftest: refusing to nest" >&2
        return 3
    fi

    # Deliberately not `local`: the cleanup trap fires after the function's
    # locals are gone, so a local would be unbound by the time rm ran.
    SELFTEST_DIR=$(mktemp -d)
    local dir="$SELFTEST_DIR"
    trap 'rm -rf "$SELFTEST_DIR"' EXIT
    export KARAKOS_SELFTEST_DIR="$dir"
    export KARAKOS_SELFTEST_BAD="9.9.9"
    export KARAKOS_SELFTEST_TOKEN="KARAKOS_CLI_OK"

    cat > "$dir/npm" <<'FAKE'
#!/usr/bin/env bash
pkg=""
for arg in "$@"; do case "$arg" in *@*) pkg="$arg";; esac; done
version="${pkg##*@}"
echo "install $version" >> "$KARAKOS_SELFTEST_DIR/npm.log"
echo "$version" > "$KARAKOS_SELFTEST_DIR/installed"
FAKE

    cat > "$dir/claude" <<'FAKE'
#!/usr/bin/env bash
version=$(cat "$KARAKOS_SELFTEST_DIR/installed")
if [[ "${1:-}" == "--version" ]]; then
    echo "$version (Claude Code)"
    exit 0
fi
cat > /dev/null
if [[ "$version" == "$KARAKOS_SELFTEST_BAD" ]]; then
    echo "Error: agent loop failed to start" >&2
    exit 1
fi
echo "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"$KARAKOS_SELFTEST_TOKEN\"}"
FAKE

    cat > "$dir/discord-notify.sh" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >> "$KARAKOS_SELFTEST_DIR/notify.log"
FAKE

    cat > "$dir/poke.sh" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >> "$KARAKOS_SELFTEST_DIR/poke.log"
FAKE

    chmod +x "$dir/npm" "$dir/claude" "$dir/discord-notify.sh" "$dir/poke.sh"

    local failures=0
    check() {
        if [[ "$2" == "$3" ]]; then
            echo "  ok   $1"
        else
            echo "  FAIL $1 (expected '$3', got '$2')"
            failures=$(( failures + 1 ))
        fi
    }
    run_under_fakes() {
        PATH="$dir:$PATH" \
        KARAKOS_NOTIFY_BIN_DIR="$dir" \
        CLAUDE_CLI_STATE_FILE="$dir/claude-cli.json" \
        CLI_VERIFY_MODEL="sonnet" CLI_VERIFY_ATTEMPTS=1 CLI_VERIFY_RETRY_DELAY=0 \
            bash "${BASH_SOURCE[0]}" >/dev/null 2>&1
    }

    echo "selftest: an upgrade that arrived on its own and breaks must be reverted"
    echo "1.0.0" > "$dir/installed"
    local rc=0
    run_under_fakes || rc=$?
    check "first run adopts what is installed" "$rc" "0"

    echo "$KARAKOS_SELFTEST_BAD" > "$dir/installed"
    : > "$dir/npm.log"
    rc=0
    run_under_fakes || rc=$?
    check "exit code says 'reverted'" "$rc" "1"
    check "installed version rolled back" "$(cat "$dir/installed")" "1.0.0"
    check "npm was told to reinstall the known-good version" \
        "$(tr '\n' ';' < "$dir/npm.log")" "install 1.0.0;"
    if grep -qi "revert" "$dir/notify.log" 2>/dev/null; then
        echo "  ok   a revert notice was posted to Discord"
    else
        echo "  FAIL no revert notice was posted to Discord"
        failures=$(( failures + 1 ))
    fi
    check "nothing was routed through the agent queue" \
        "$([[ -f "$dir/poke.log" ]] && echo used || echo unused)" "unused"

    echo "selftest: an upgrade that works must be adopted, not rolled back"
    echo "2.0.0" > "$dir/installed"
    : > "$dir/npm.log"
    : > "$dir/notify.log"
    rc=0
    run_under_fakes || rc=$?
    check "exit code says 'fine'" "$rc" "0"
    check "the new version was kept" "$(cat "$dir/installed")" "2.0.0"
    check "npm was not touched" "$(wc -c < "$dir/npm.log" | tr -d ' ')" "0"
    check "no notice posted" "$(wc -c < "$dir/notify.log" | tr -d ' ')" "0"

    if (( failures == 0 )); then
        echo "selftest: PASS — the drift rollback is armed"
        return 0
    fi
    echo "selftest: FAIL — $failures check(s) failed; the drift rollback is NOT trustworthy"
    return 1
}

main() {
    local mode="run"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --selftest) mode="selftest"; shift ;;
            --channel) ALERT_CHANNEL="${2:-}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) echo "Unknown argument: $1" >&2; usage >&2; exit 3 ;;
        esac
    done

    if [[ "$mode" == "selftest" ]]; then
        run_selftest
    else
        run_watchdog
    fi
}

main "$@"
