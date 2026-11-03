#!/usr/bin/env bash
# self-upgrade.sh — ask the host to upgrade this Docker install (runs INSIDE the container).
#
# The container has no Docker socket, on purpose. So it cannot upgrade itself.
# Instead it drops a request into the bind-mounted .karakos/ directory
# (host ./.karakos = container $WORKSPACE_ROOT/.karakos), and a small agent on
# the host (bin/upgrade-agent.sh, run every minute from cron by
# `make install-upgrade-agent`) picks it up, upgrades, health-checks, rolls
# back on failure and writes the result to status.json. On the next container
# start, bin/entrypoint.sh runs `self-upgrade.sh report` to tell the requester.
#
# Files under .karakos/upgrade/:
#   request.json     {"version","requested_by","requested_at"}
#   status.json      {"state","version_from","version_to","started_at",
#                     "finished_at","detail","reported", + "requested_by"}
#   host.log         append-only log of the host agent's runs
#   host-agent.seen  touched by the host agent on every run (the heartbeat)
#   host-agent.lock  flock file
#
# Usage:
#   self-upgrade.sh request [VERSION]   VERSION: latest (default) | vX.Y | vX.Y.Z
#   self-upgrade.sh status
#   self-upgrade.sh report              tell the requester how it went; never fails
#
# Exit codes (request): 0 queued; 2 bad version; 3 an upgrade is already
# pending/running; 4 request written but no host agent seen recently.

set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPGRADE_DIR="$WORKSPACE_ROOT/.karakos/upgrade"
REQUEST="$UPGRADE_DIR/request.json"
STATUS="$UPGRADE_DIR/status.json"
SEEN="$UPGRADE_DIR/host-agent.seen"
VERSION_RE='^(latest|v[0-9]+\.[0-9]+(\.[0-9]+)?)$'
# Overridable only so the tests can use a recording stand-in.
NOTIFY_BIN_DIR="${KARAKOS_NOTIFY_BIN_DIR:-$SCRIPT_DIR}"
ALERT_CHANNEL="${UPDATE_ALERT_CHANNEL:-signals}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

state_of() {
    [[ -f "$STATUS" ]] || return 0
    jq -r 'if type == "object" and (.state | type) == "string" then .state else empty end' \
        "$STATUS" 2>/dev/null || true
}

cmd_request() {
    local version="${1:-latest}"
    if [[ ! "$version" =~ $VERSION_RE ]]; then
        echo "self-upgrade: invalid version '$version' (use latest, vX.Y or vX.Y.Z)" >&2
        return 2
    fi

    local state
    state=$(state_of)
    if [[ "$state" == "pending" || "$state" == "running" ]]; then
        echo "self-upgrade: an upgrade is already $state (see: self-upgrade.sh status)." >&2
        if [[ "$state" == "pending" ]]; then
            echo "self-upgrade: if no host agent is picking it up, delete $STATUS and $REQUEST to clear it." >&2
        fi
        return 3
    fi

    mkdir -p "$UPGRADE_DIR"
    local by="${KARAKOS_AGENT_NAME:-${USER:-unknown}}" at tmp
    at=$(now)

    tmp="$REQUEST.tmp"
    jq -n --arg v "$version" --arg by "$by" --arg at "$at" \
        '{version: $v, requested_by: $by, requested_at: $at}' > "$tmp"
    mv "$tmp" "$REQUEST"

    tmp="$STATUS.tmp"
    jq -n --arg v "$version" --arg by "$by" --arg at "$at" \
        '{state: "pending", version_from: "", version_to: $v, started_at: "",
          finished_at: "", detail: ("requested by " + $by + " at " + $at),
          reported: false, requested_by: $by}' > "$tmp"
    mv "$tmp" "$STATUS"

    if [[ ! -f "$SEEN" ]] || [[ -z "$(find "$SEEN" -mmin -10 2>/dev/null)" ]]; then
        echo 'self-upgrade: host upgrade agent not detected; run `make install-upgrade-agent` on the host once' >&2
        return 4
    fi
    echo "self-upgrade: upgrade to $version requested; the host agent picks it up within a minute."
}

cmd_status() {
    if [[ -f "$STATUS" ]]; then
        cat "$STATUS"
    else
        echo "no upgrade requested"
    fi
}

# Never fails the caller: the entrypoint runs this, in the background, on every
# start. Waits up to KARAKOS_UPGRADE_REPORT_WAIT seconds (default 600) while the
# state is `running`.
cmd_report() {
    local state reported
    state=$(state_of)
    # The container starts mid-upgrade: the host agent flips `running` to
    # done/failed/needs-migrate only after its health check (and, on rollback,
    # after the rollback `make up`). Wait for that, so the boot-time report
    # lands. `pending` is not waited on: with no host agent it never moves.
    local waited=0 limit="${KARAKOS_UPGRADE_REPORT_WAIT:-600}"
    while [[ "$state" == "running" ]] && (( waited < limit )); do
        sleep 2
        waited=$(( waited + 2 ))
        state=$(state_of)
    done
    case "$state" in done|failed|needs-migrate) ;; *) return 0 ;; esac
    reported=$(jq -r '.reported // false' "$STATUS" 2>/dev/null || echo true)
    [[ "$reported" == "false" ]] || return 0

    local from to detail by msg
    from=$(jq -r '.version_from // ""' "$STATUS")
    to=$(jq -r '.version_to // ""' "$STATUS")
    detail=$(jq -r '.detail // ""' "$STATUS")
    by=$(jq -r '.requested_by // ""' "$STATUS")
    case "$state" in
        done) msg="Karakos upgrade done: ${from:-?} -> ${to:-?}. $detail" ;;
        needs-migrate) msg="Karakos upgrade needs a migration: ${from:-?} -> ${to:-?} exited 78; the host rolled back. $detail Run \`karakos migrate\` on the host, then request the upgrade again." ;;
        *) msg="Karakos upgrade failed: ${from:-?} -> ${to:-?}. $detail" ;;
    esac

    local poke="$NOTIFY_BIN_DIR/poke.sh"
    [[ -f "$poke" ]] || return 0

    local sent=false
    if [[ -n "$by" && "$by" != "unknown" ]] && \
        bash "$poke" --agent "$by" --source "self-upgrade" \
            --reply-channel "$ALERT_CHANNEL" "$msg" >/dev/null 2>&1; then
        sent=true
    elif bash "$poke" --source "self-upgrade" \
            --reply-channel "$ALERT_CHANNEL" "$msg" >/dev/null 2>&1; then
        # requested_by was not a registered agent (e.g. $USER): the primary hears it.
        sent=true
    fi
    $sent || return 0

    local tmp="$STATUS.tmp"
    if jq '.reported = true' "$STATUS" > "$tmp"; then
        mv "$tmp" "$STATUS"
    fi
}

main() {
    local sub="${1:-}"
    [[ $# -eq 0 ]] || shift
    case "$sub" in
        request) cmd_request "$@" ;;
        status) cmd_status ;;
        report) cmd_report "$@" || true; exit 0 ;;
        *)
            echo "Usage: self-upgrade.sh request [latest|vX.Y|vX.Y.Z] | status | report" >&2
            exit 1
            ;;
    esac
}

main "$@"
