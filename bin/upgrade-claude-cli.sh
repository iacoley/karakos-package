#!/usr/bin/env bash
# upgrade-claude-cli.sh — move the Claude CLI behind a verified rollback.
#
# The failure this exists for: the agent loop runs on a CLI this project does
# not release. A bad upstream release installs cleanly, `claude --version`
# answers, supervisord reports every process up, /health returns 200 — and no
# message is ever answered again. Every install that upgraded that week goes
# quiet at the same time, and the maintainer hears nothing, because a quiet
# install emits nothing by definition.
#
# So this script never treats "installed" as "working":
#
# **The check is a turn, not a version string.** It feeds one message through
# `--input-format stream-json --output-format stream-json`, the exact wire
# agent-server.py depends on, and requires a `result` event back. A release
# that renamed that event, or broke stdin framing, passes `--version` and
# fails here — which is the whole point.
#
# **The rollback target is recorded before anything is touched.** If the
# currently installed version cannot be read, the upgrade is refused outright:
# an upgrade with no known way back is the thing being guarded against.
#
# **The notice goes direct to Discord, never through poke.sh.** poke.sh queues
# a message FOR AN AGENT. If the reason we are here is that agents cannot
# complete a turn, that queue is exactly the place nobody will read. Same
# reasoning as bin/wedge-check.py.
#
# **A failed verify is retried before it is believed.** One dropped API call
# is not a bad release, and reverting a good version on a network blip would
# make this script a source of outages rather than a cure for them.
#
# Modes:
#   (default)              upgrade to --to VERSION, verify, revert if broken
#   --verify-only          run the probe turn against what is installed
#   --install VERSION      install a version, no verify (used by the watchdog)
#   --print-version        print the installed CLI version
#   --selftest             prove the revert path still fires (see below)
#
# Exit codes:
#   0  upgraded and verified  (or: verify passed / install succeeded)
#   1  verification failed; the upgrade was reverted and a notice was posted
#   2  verification failed AND the revert failed — this install may be dark
#   3  could not run at all (no rollback target, install error, bad usage)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"

# Which directory discord-notify.sh is read from. Overridable so the tests can
# watch the real call happen against a recording stand-in; the command name and
# argument shape stay the production ones either way.
NOTIFY_BIN_DIR="${KARAKOS_NOTIFY_BIN_DIR:-$SCRIPT_DIR}"

NPM_PACKAGE="${CLAUDE_CLI_PACKAGE:-@anthropic-ai/claude-code}"
ALERT_CHANNEL="${CLI_ALERT_CHANNEL:-signals}"
VERIFY_TIMEOUT="${CLI_VERIFY_TIMEOUT:-180}"
VERIFY_ATTEMPTS="${CLI_VERIFY_ATTEMPTS:-2}"
VERIFY_RETRY_DELAY="${CLI_VERIFY_RETRY_DELAY:-5}"
PROBE_TOKEN="KARAKOS_CLI_OK"
PROBE_PROMPT="Reply with exactly ${PROBE_TOKEN} and nothing else."

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] cli-upgrade: $*"; }

usage() {
    sed -n '2,43p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# The model the probe turn runs on. Deliberately the primary agent's own model
# rather than a cheaper fixed one: an alias this install's CLI does not accept
# would fail the probe and revert a perfectly good upgrade, and the model the
# agents actually use is the one whose breakage matters.
verify_model() {
    local cfg="$WORKSPACE_ROOT/config/agents.json"
    if [[ -n "${CLI_VERIFY_MODEL:-}" ]]; then
        printf '%s' "$CLI_VERIFY_MODEL"
        return 0
    fi
    if [[ -f "$cfg" ]]; then
        local m
        m=$(python3 -c '
import json, sys
try:
    agents = json.load(open(sys.argv[1])).get("agents") or {}
except Exception:
    sys.exit(0)
for _, info in agents.items():
    if isinstance(info, dict) and info.get("model"):
        print(info["model"])
        break
' "$cfg" 2>/dev/null || true)
        if [[ -n "$m" ]]; then
            printf '%s' "$m"
            return 0
        fi
    fi
    printf 'sonnet'
}

installed_version() {
    local raw
    raw=$(claude --version 2>/dev/null | head -1 || true)
    local v
    v=$(printf '%s' "$raw" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*' | head -1 || true)
    [[ -n "$v" ]] || return 1
    printf '%s' "$v"
}

npm_install() {
    local version="$1"
    log "npm install -g ${NPM_PACKAGE}@${version}"
    npm install -g "${NPM_PACKAGE}@${version}" >/dev/null 2>&1
}

# One probe turn. Success means a `result` event came back, it did not carry
# is_error, and the model's text made it through intact.
#
# The probe is text-only on purpose. Driving a tool call would catch more, but
# it would also fail for reasons that are not the CLI's fault — a denied
# permission, a missing binary — and a rollback guard that reverts for the
# wrong reason gets switched off.
probe_turn_once() {
    local model="$1"
    local payload
    payload=$(python3 -c '
import json, sys
print(json.dumps({"type": "user",
                  "message": {"role": "user", "content": sys.argv[1]}}))
' "$PROBE_PROMPT")

    local out
    if ! out=$(printf '%s\n' "$payload" | timeout "$VERIFY_TIMEOUT" claude -p \
            --input-format stream-json \
            --output-format stream-json \
            --verbose \
            --model "$model" \
            --max-turns 1 \
            --dangerously-skip-permissions 2>/dev/null); then
        return 1
    fi

    printf '%s\n' "$out" | python3 -c '
import json, sys

token = sys.argv[1]
saw_result = False
text = []

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if not isinstance(event, dict):
        continue
    etype = event.get("type")
    if etype == "assistant":
        blocks = (event.get("message") or {}).get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text.append(block.get("text") or "")
    elif etype == "result":
        if event.get("is_error"):
            sys.exit(1)
        saw_result = True
        text.append(event.get("result") or "")

sys.exit(0 if saw_result and token in "".join(text) else 1)
' "$PROBE_TOKEN"
}

verify_turn() {
    local model
    model=$(verify_model)
    local attempt=1
    while true; do
        if probe_turn_once "$model"; then
            log "probe turn completed (model=$model, attempt $attempt)"
            return 0
        fi
        if (( attempt >= VERIFY_ATTEMPTS )); then
            log "probe turn failed $attempt/$VERIFY_ATTEMPTS (model=$model)"
            return 1
        fi
        log "probe turn failed $attempt/$VERIFY_ATTEMPTS, retrying"
        attempt=$(( attempt + 1 ))
        [[ "$VERIFY_RETRY_DELAY" == "0" ]] || sleep "$VERIFY_RETRY_DELAY"
    done
}

# Direct to Discord with the bot token. NOT the agent queue — see the header.
notify() {
    local message="$1"
    local notifier="$NOTIFY_BIN_DIR/discord-notify.sh"
    if [[ ! -f "$notifier" ]]; then
        echo "cli-upgrade: no discord-notify.sh, notice not sent: $message" >&2
        return 1
    fi
    if ! bash "$notifier" "$ALERT_CHANNEL" "$message" >/dev/null 2>&1; then
        echo "cli-upgrade: notice failed to post: $message" >&2
        return 1
    fi
    log "posted notice to #$ALERT_CHANNEL"
}

do_upgrade() {
    local target="$1"

    local previous
    if ! previous=$(installed_version); then
        log "cannot read the installed CLI version — refusing to upgrade with no rollback target"
        return 3
    fi

    log "upgrading $NPM_PACKAGE: $previous -> $target"
    if ! npm_install "$target"; then
        log "install of $target failed; $previous is still in place"
        return 3
    fi

    local now
    now=$(installed_version) || now="$target"
    log "installed $now, running probe turn"

    if verify_turn; then
        log "upgrade to $now verified"
        return 0
    fi

    log "$now cannot complete a turn — reverting to $previous"
    if ! npm_install "$previous"; then
        notify "🚨 Claude CLI upgrade to \`$now\` failed verification and the revert to \`$previous\` **also failed**. This install cannot answer messages until the CLI is repaired by hand: \`npm install -g ${NPM_PACKAGE}@${previous}\`." || true
        return 2
    fi

    if verify_turn; then
        notify "⚠️ Claude CLI upgrade reverted. \`$previous\` → \`$now\` could not complete a turn, so \`$previous\` was reinstalled and now answers normally. Version \`$now\` was not kept — if it lands again, the watchdog will roll it back again." || true
    else
        # Honest reporting matters more than a clean story: if the old version
        # cannot complete a turn either, the upgrade was probably not the
        # fault, and sending "all fixed" would send the operator hunting in
        # the wrong place.
        notify "⚠️ Claude CLI upgrade reverted (\`$now\` → \`$previous\`), but a turn still fails on \`$previous\`. The fault may not be the upgrade — check API credentials and connectivity." || true
    fi
    return 1
}

# --------------------------------------------------------------------------
# Selftest — a positive control for the revert path.
#
# A rollback that has never fired is a rollback nobody knows is wired up. This
# builds a fake npm/claude/discord-notify on PATH, re-runs this very script
# against a version whose CLI cannot complete a turn, and fails unless the
# revert actually happened and a notice actually went out. It also runs the
# negative control — a good version must NOT be reverted and must NOT page.
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
    export KARAKOS_SELFTEST_TOKEN="$PROBE_TOKEN"
    echo "1.0.0" > "$dir/installed"

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
    # What a bad release looks like from outside: installs fine, answers
    # --version, and then never produces a result event.
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

    echo "selftest: a broken upgrade must be reverted and reported"
    local rc=0
    PATH="$dir:$PATH" KARAKOS_NOTIFY_BIN_DIR="$dir" CLI_VERIFY_MODEL="sonnet" \
        CLI_VERIFY_ATTEMPTS=1 CLI_VERIFY_RETRY_DELAY=0 \
        bash "${BASH_SOURCE[0]}" --to "$KARAKOS_SELFTEST_BAD" >/dev/null 2>&1 || rc=$?
    check "exit code says 'reverted'" "$rc" "1"
    check "installed version rolled back" "$(cat "$dir/installed")" "1.0.0"
    check "npm was told to install the bad version, then the old one" \
        "$(tr '\n' ';' < "$dir/npm.log")" "install 9.9.9;install 1.0.0;"
    if grep -qi "revert" "$dir/notify.log" 2>/dev/null; then
        echo "  ok   a revert notice was posted to Discord"
    else
        echo "  FAIL no revert notice was posted to Discord"
        failures=$(( failures + 1 ))
    fi
    check "nothing was routed through the agent queue" \
        "$([[ -f "$dir/poke.log" ]] && echo used || echo unused)" "unused"

    echo "selftest: a working upgrade must be kept and must not page"
    : > "$dir/npm.log"
    : > "$dir/notify.log"
    rc=0
    PATH="$dir:$PATH" KARAKOS_NOTIFY_BIN_DIR="$dir" CLI_VERIFY_MODEL="sonnet" \
        CLI_VERIFY_ATTEMPTS=1 CLI_VERIFY_RETRY_DELAY=0 \
        bash "${BASH_SOURCE[0]}" --to "2.0.0" >/dev/null 2>&1 || rc=$?
    check "exit code says 'upgraded'" "$rc" "0"
    check "the new version was kept" "$(cat "$dir/installed")" "2.0.0"
    check "no second npm install" "$(tr '\n' ';' < "$dir/npm.log")" "install 2.0.0;"
    check "no notice posted" "$(wc -c < "$dir/notify.log" | tr -d ' ')" "0"

    if (( failures == 0 )); then
        echo "selftest: PASS — the rollback guard is armed"
        return 0
    fi
    echo "selftest: FAIL — $failures check(s) failed; the rollback guard is NOT trustworthy"
    return 1
}

main() {
    local mode="upgrade"
    local target="latest"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --to)
                target="${2:-}"
                [[ -n "$target" ]] || { echo "--to needs a version" >&2; exit 3; }
                shift 2
                ;;
            --install)
                mode="install"
                target="${2:-}"
                [[ -n "$target" ]] || { echo "--install needs a version" >&2; exit 3; }
                shift 2
                ;;
            --verify-only) mode="verify"; shift ;;
            --print-version) mode="print-version"; shift ;;
            --selftest) mode="selftest"; shift ;;
            --channel) ALERT_CHANNEL="${2:-}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) echo "Unknown argument: $1" >&2; usage >&2; exit 3 ;;
        esac
    done

    case "$mode" in
        verify)
            verify_turn
            ;;
        install)
            npm_install "$target" || { log "install of $target failed"; return 3; }
            log "installed $target"
            ;;
        print-version)
            installed_version || return 3
            echo
            ;;
        selftest)
            run_selftest
            ;;
        upgrade)
            do_upgrade "$target"
            ;;
    esac
}

main "$@"
