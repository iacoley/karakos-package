#!/usr/bin/env bash
# upgrade-agent.sh — host-side half of the Docker self-upgrade (runs ON THE HOST).
#
# Run every minute from the invoking user's crontab (`make install-upgrade-agent`),
# from the checkout root. The container has no Docker socket, so it asks for an
# upgrade by writing .karakos/upgrade/request.json (bin/self-upgrade.sh request);
# this script picks it up, runs the documented 2.x upgrade
# (git pull, make pull, make down, make up), health-checks the container, rolls
# back on failure, and writes .karakos/upgrade/status.json. The entrypoint
# reports the result to the requesting agent on the next container start.
#
# It never runs `karakos migrate`. A container that exits 78 (needs a schema
# migration) is rolled back and reported as `needs-migrate` for a human to run.
#
# Environment (tests only): KARAKOS_UPGRADE_HEALTH_TIMEOUT (default 180 s) and
# KARAKOS_UPGRADE_HEALTH_INTERVAL (default 5 s).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# cron starts with a minimal PATH.
PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:/snap/bin"

UPGRADE_DIR=".karakos/upgrade"
REQUEST="$UPGRADE_DIR/request.json"
STATUS="$UPGRADE_DIR/status.json"
LOG="$UPGRADE_DIR/host.log"
ENV_FILE="config/.env"
IMAGE="ghcr.io/mcarmody/karakos"
VERSION_RE='^(latest|v[0-9]+\.[0-9]+(\.[0-9]+)?)$'
HEALTH_TIMEOUT="${KARAKOS_UPGRADE_HEALTH_TIMEOUT:-180}"
HEALTH_INTERVAL="${KARAKOS_UPGRADE_HEALTH_INTERVAL:-5}"
COMPOSE=(docker compose -f config/docker-compose.yml --env-file "$ENV_FILE")

mkdir -p "$UPGRADE_DIR"
touch "$UPGRADE_DIR/host-agent.seen"

exec 9>"$UPGRADE_DIR/host-agent.lock"
flock -n 9 || exit 0

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '[%s] upgrade-agent: %s\n' "$(now)" "$*" >> "$LOG"; }

state_of() {
    [[ -f "$STATUS" ]] || return 0
    jq -r 'if type == "object" and (.state | type) == "string" then .state else empty end' \
        "$STATUS" 2>/dev/null || true
}

# write_status STATE DETAIL [key=value ...] — merge into status.json, keeping
# the fields the container wrote (requested_by, reported, ...).
write_status() {
    local state="$1" detail="$2" tmp="$STATUS.tmp" base='{}'
    shift 2
    [[ -f "$STATUS" ]] && base=$(jq -c 'if type == "object" then . else {} end' "$STATUS" 2>/dev/null || echo '{}')
    local args=(--arg state "$state" --arg detail "$detail")
    local expr='. + {state: $state, detail: $detail}'
    local kv
    for kv in "$@"; do
        args+=(--arg "f_${kv%%=*}" "${kv#*=}")
        expr+=" | .${kv%%=*} = \$f_${kv%%=*}"
    done
    jq "${args[@]}" "$expr" <<< "$base" > "$tmp"
    mv "$tmp" "$STATUS"
}

# Replace the KARAKOS_VERSION line in config/.env, or append it if absent.
set_pin() {
    local value="$1" tmp="$ENV_FILE.tmp"
    if grep -qE '^[[:space:]]*KARAKOS_VERSION=' "$ENV_FILE" 2>/dev/null; then
        awk -v v="$value" '/^[[:space:]]*KARAKOS_VERSION=/ { print "KARAKOS_VERSION=" v; next } { print }' \
            "$ENV_FILE" > "$tmp"
    else
        { cat "$ENV_FILE" 2>/dev/null || true; printf 'KARAKOS_VERSION=%s\n' "$value"; } > "$tmp"
    fi
    cat "$tmp" > "$ENV_FILE"   # keep the file's mode and ownership
    rm -f "$tmp"
}

current_pin() {
    local line=""
    if [[ -f "$ENV_FILE" ]]; then
        line=$(grep -E '^[[:space:]]*KARAKOS_VERSION=' "$ENV_FILE" | tail -n1 || true)
    fi
    line="${line#*=}"
    line="${line//[\"\' $'\r\t']/}"
    printf '%s' "$line"
}

# step NAME CMD... — run a command with its output in host.log; record the name on failure.
FAILED_STEP=""
step() {
    local name="$1"
    shift
    log "step: $name ($*)"
    if ! "$@" >> "$LOG" 2>&1; then
        FAILED_STEP="$name"
        log "step failed: $name"
        return 1
    fi
}

# Prints healthy | needs-migrate | waiting
health_once() {
    local out info
    out=$("${COMPOSE[@]}" ps --format json karakos 2>>"$LOG") || { echo waiting; return; }
    info=$(jq -rs 'flatten | map(select(type == "object")) | .[0] // {}
                   | "\(.ExitCode // 0) \(.Health // "")"' <<< "$out" 2>/dev/null) || { echo waiting; return; }
    case "$info" in
        78\ *) echo needs-migrate ;;
        *\ healthy) echo healthy ;;
        *) echo waiting ;;
    esac
}

wait_healthy() {
    local waited=0 h
    while :; do
        h=$(health_once)
        [[ "$h" == waiting ]] || { echo "$h"; return; }
        (( waited < HEALTH_TIMEOUT )) || { echo timeout; return; }
        sleep "$HEALTH_INTERVAL"
        waited=$(( waited + HEALTH_INTERVAL ))
    done
}

# --- anything to do? -------------------------------------------------------

state=$(state_of)
if [[ "$state" == "running" ]]; then
    # We hold the lock, so no other run is alive: a previous run died mid-way.
    log "status was 'running' with no live run; marking it failed"
    write_status failed "an earlier host agent run was interrupted; check \`make ps\` and host.log" \
        "finished_at=$(now)"
    rm -f "$REQUEST"
    exit 0
fi
[[ -f "$REQUEST" && "$state" == "pending" ]] || exit 0

# --- validate --------------------------------------------------------------

version=$(jq -r 'if type == "object" and (.version | type) == "string" then .version else "" end' \
    "$REQUEST" 2>/dev/null || true)
if [[ ! "$version" =~ $VERSION_RE ]]; then
    log "rejected request with invalid version"
    write_status failed "invalid version" "finished_at=$(now)"
    rm -f "$REQUEST"
    exit 0
fi

# --- run -------------------------------------------------------------------

version_from=$(current_pin)
version_from="${version_from:-latest}"
had_pin=false
[[ "$version_from" != latest ]] && had_pin=true
write_status running "upgrading ${version_from} -> ${version}" \
    "started_at=$(now)" "version_from=$version_from" "version_to=$version" "finished_at="
log "upgrade ${version_from} -> ${version} started"

FINISHED=false
on_exit() {
    $FINISHED && return 0
    write_status failed "host agent crashed during step '${FAILED_STEP:-unknown}'; check host.log" \
        "finished_at=$(now)" || true
    rm -f "$REQUEST"
}
trap on_exit EXIT

finish() {   # finish STATE DETAIL [version_to]
    local state="$1" detail="$2" to="${3:-$version}"
    write_status "$state" "$detail" "finished_at=$(now)" "version_to=$to"
    rm -f "$REQUEST"
    FINISHED=true
    log "$state: $detail"
}

# Anything past this point has changed nothing until `git pull` succeeds.
if ! step "rollback tag" docker image tag "$IMAGE:$version_from" "$IMAGE:rollback"; then
    finish failed "could not tag $IMAGE:$version_from as rollback; nothing was changed"
    exit 0
fi
if ! step "git pull" git pull --ff-only origin main; then
    finish failed "git pull --ff-only failed (dirty tree or non-fast-forward); nothing was changed"
    exit 0
fi

note=""
version_to="$version"
if [[ "$version" != latest ]]; then
    set_pin "$version"
    log "set KARAKOS_VERSION=$version in $ENV_FILE"
elif $had_pin; then
    note=" KARAKOS_VERSION=$version_from is pinned and was left alone."
    version_to="$version_from"
    log "latest requested but KARAKOS_VERSION=$version_from is pinned; leaving the pin"
fi

outcome=""
for target in "pull" "down" "up"; do
    if ! step "make $target" make "$target"; then
        outcome="failed"
        break
    fi
done
if [[ -z "$outcome" ]]; then
    case "$(wait_healthy)" in
        healthy) outcome="healthy" ;;
        needs-migrate) outcome="needs-migrate"; FAILED_STEP="health check (container exited 78)" ;;
        *) outcome="failed"; FAILED_STEP="health check (not healthy after ${HEALTH_TIMEOUT}s)" ;;
    esac
fi

if [[ "$outcome" == healthy ]]; then
    finish "done" "upgraded ${version_from} -> ${version_to}.${note}" "$version_to"
    exit 0
fi

# Roll back: the previous image is tagged `rollback`, so point the pin at it.
log "rolling back after: $FAILED_STEP"
set_pin rollback
old="${version_from}"
$had_pin || old="unset (latest)"
rb="rolled back to image tag 'rollback'; KARAKOS_VERSION in $ENV_FILE is now 'rollback' (was $old)"
if ! step "rollback make up" make up; then
    rb="$rb, but 'make up' failed during rollback: check \`make ps\`"
fi
if [[ "$outcome" == needs-migrate ]]; then
    finish needs-migrate "$FAILED_STEP: ${version} needs a schema migration, run \`karakos migrate\` on the host (not run automatically); $rb"
else
    finish failed "$FAILED_STEP failed; $rb"
fi
