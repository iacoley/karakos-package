#!/usr/bin/env bash
# check-updates.sh — weekly check for a newer Karakos release.
#
# Scheduled Mondays 05:00 by bin/scheduler.py. Two things it has to get right,
# and it used to get both wrong (#152).
#
# **What version is this install running?** The image tag, and nothing else.
# config/docker-compose.yml resolves
# `ghcr.io/mcarmody/karakos:${KARAKOS_VERSION:-latest}`, and `env_file: .env`
# puts that same KARAKOS_VERSION inside the container — so reading it here,
# defaulting to `latest` exactly as compose does, is the one answer that cannot
# disagree with the code actually running. Every other candidate was checked
# and is wrong:
#
#   - `$WORKSPACE_ROOT/package.json` — what this script used to read. There is
#     no package.json at the workspace root; the only one is
#     dashboard/package.json. Under `set -e` the `cat` killed the script on its
#     tenth line, so the weekly check had never completed a single run.
#   - `.karakos/config.json` — setup.sh writes `"version": "1.0.0"` as a string
#     literal, on every install, of every release, and never rewrites it on
#     upgrade. It stamps the config schema, not the release.
#   - `dashboard/package.json` — also a frozen "1.0.0", and it versions the
#     Next.js app rather than the release.
#   - a git tag — .dockerignore excludes .git and bin/entrypoint.sh runs a
#     fresh `git init` in /workspace, so a deployed install has no tags at all.
#
# **Who hears about it?** bin/poke.sh, to the primary agent, replying in
# #signals — deliberately NOT the direct-to-Discord path that
# bin/cli-upgrade-watchdog.sh takes. That script bypasses the agent queue
# because the thing it reports is that agents cannot complete a turn, so a
# queued notice would land in the one place nobody can read it. An available
# update is not an outage: agents are answering normally, and an agent can do
# something with this that a raw webhook post cannot — read the release notes,
# say what actually changed, and be asked "should we?" in the same thread. If
# the agent server happens to be down, poke.sh spools the message and
# bin/flush-deferred-messages.py re-fires it, so the notice is not lost either.
#
# **The `latest` case.** On a default install KARAKOS_VERSION is unset, the
# running tag is literally `latest`, and there is no version number to compare.
# Comparing the string "latest" to "v1.4" is not a comparison. What is true and
# worth saying is that a release exists and that `latest` only moves when the
# image is pulled — so the notice reports the release and the pull command, and
# a state file keeps it to once per release rather than every Monday forever.
#
# **Pinning to a minor tag is not being behind.** .github/workflows/release.yml
# publishes a v1.3.1 build as `v1.3` and `v1` as well, so KARAKOS_VERSION=v1.3
# already carries the newest 1.3.x. Comparison is therefore done at the
# precision the operator actually pinned.
#
# Exit codes:
#   0  the check completed — up to date, pinned ahead, or an update announced
#   1  the check could not complete — the releases API was unreachable or
#      unreadable, or an available update could not be announced. Nobody is
#      paged for that (a network blip is not news); bin/scheduler.py logs it,
#      which is what keeps a permanently broken checker from going quiet for
#      another month.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-${KARAKOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

KARAKOS_REPO="${KARAKOS_REPO:-mcarmody/karakos-package}"
RELEASES_URL="${KARAKOS_RELEASES_URL:-https://api.github.com/repos/${KARAKOS_REPO}/releases/latest}"

# Overridable only so the tests can watch the real call happen against a
# recording stand-in; the command name and arguments stay production ones.
NOTIFY_BIN_DIR="${KARAKOS_NOTIFY_BIN_DIR:-$SCRIPT_DIR}"

STATE_FILE="${KARAKOS_UPDATE_STATE_FILE:-$WORKSPACE_ROOT/data/health/karakos-update.json}"
ALERT_CHANNEL="${UPDATE_ALERT_CHANNEL:-signals}"
FORCE=false

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] check-updates: $*"; }

usage() {
    cat <<'EOF'
Usage: check-updates.sh [--force] [--channel NAME]

Checks the GitHub releases API for a newer Karakos release than the image tag
this install is running (${KARAKOS_VERSION:-latest}), and pokes the primary
agent to report it in #signals.

  --force          re-announce a release that was already announced
  --channel NAME   reply channel for the notice (default: signals)
  -h, --help       this text

Exit 0 = checked; exit 1 = could not check, or could not announce.
EOF
}

# --------------------------------------------------------------------------
# Version handling
# --------------------------------------------------------------------------

# The tag `docker compose` resolves for this install. Not a file on disk:
# every version-bearing file in the tree is frozen at the installer's 1.0.0.
installed_version() {
    local v="${KARAKOS_VERSION:-}"
    v="${v//[$'\n\r\t ']/}"
    printf '%s' "${v:-latest}"
}

is_version() { [[ "$1" =~ ^v?[0-9]+(\.[0-9]+)*$ ]]; }

strip_v() { printf '%s' "${1#v}"; }

# How many dot-separated components an operator pinned. `v1.3` is a moving tag
# that carries the newest 1.3.x, so a v1.3.1 release does not make it stale.
precision() { awk -F. '{print NF}' <<< "$1"; }

truncate_version() { cut -d. -f"1-$2" <<< "$1"; }

# newer A B -> 0 when A is strictly newer than B. Both bare (no leading v).
newer() {
    [[ "$1" != "$2" ]] || return 1
    [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" == "$1" ]]
}

# --------------------------------------------------------------------------
# State — which release we have already told someone about
# --------------------------------------------------------------------------

read_last_announced() {
    [[ -f "$STATE_FILE" ]] || return 0
    jq -r 'if type == "object" and (.last_announced | type) == "string"
           then .last_announced else empty end' \
        "$STATE_FILE" 2>/dev/null || true
}

write_last_announced() {
    local version="$1" tmp
    mkdir -p "$(dirname "$STATE_FILE")"
    tmp="${STATE_FILE}.tmp"
    jq -n --arg v "$version" --arg at "$(date -Iseconds)" \
        '{last_announced: $v, announced_at: $at}' > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

# --------------------------------------------------------------------------
# Notification — through the agent queue, on purpose. See the header.
# --------------------------------------------------------------------------

notify() {
    local message="$1"
    local poke="$NOTIFY_BIN_DIR/poke.sh"
    if [[ ! -f "$poke" ]]; then
        echo "check-updates: no poke.sh at $poke, notice not sent: $message" >&2
        return 1
    fi
    if ! bash "$poke" --source "update-checker" \
            --reply-channel "$ALERT_CHANNEL" "$message"; then
        echo "check-updates: notice failed to send: $message" >&2
        return 1
    fi
    log "notice queued for the primary agent, replying in #$ALERT_CHANNEL"
}

fetch_latest_release() {
    curl -sSf --max-time 20 \
        -H 'Accept: application/vnd.github+json' \
        "$RELEASES_URL" 2>/dev/null
}

json_string() {
    local field="$2"
    jq -r --arg f "$field" \
        'if type == "object" and (.[$f] | type) == "string"
         then .[$f] else empty end' <<< "$1" 2>/dev/null || true
}

# --------------------------------------------------------------------------

run_check() {
    local current latest_json latest_tag release_url
    current=$(installed_version)
    log "running image tag: $current (from \${KARAKOS_VERSION:-latest}, what docker compose resolves)"

    if ! latest_json=$(fetch_latest_release); then
        echo "check-updates: could not reach the releases API at $RELEASES_URL" >&2
        return 1
    fi

    latest_tag=$(json_string "$latest_json" tag_name)
    if [[ -z "$latest_tag" ]]; then
        echo "check-updates: no tag_name in the response from $RELEASES_URL" >&2
        return 1
    fi

    release_url=$(json_string "$latest_json" html_url)
    [[ -n "$release_url" ]] || \
        release_url="https://github.com/${KARAKOS_REPO}/releases/tag/${latest_tag}"

    local pinned=false
    is_version "$current" && pinned=true

    if $pinned && is_version "$latest_tag"; then
        local cur_n lat_n depth
        cur_n=$(strip_v "$current")
        lat_n=$(strip_v "$latest_tag")
        depth=$(precision "$cur_n")
        lat_n=$(truncate_version "$lat_n" "$depth")

        if [[ "$cur_n" == "$lat_n" ]]; then
            log "already on the newest release ($latest_tag; pinned $current)"
            return 0
        fi
        if ! newer "$lat_n" "$cur_n"; then
            log "pinned ahead of the newest published release (pinned $current, newest $latest_tag) — nothing to do"
            return 0
        fi
    fi

    local last_announced
    last_announced=$(read_last_announced)
    if [[ "$last_announced" == "$latest_tag" && "$FORCE" != true ]]; then
        log "$latest_tag has already been announced — not repeating it (--force to re-send)"
        return 0
    fi

    local message
    if $pinned; then
        log "update available: $latest_tag (this install is pinned to $current)"
        message="📦 Karakos **${latest_tag}** is out — this install is pinned to \`${current}\`. To upgrade: set \`KARAKOS_VERSION=${latest_tag}\` in \`config/.env\`, then \`docker compose pull && docker compose up -d\`. Release notes: ${release_url}"
    elif [[ "$current" == "latest" ]]; then
        log "release available: $latest_tag (this install tracks \`latest\`, so the tag alone cannot say whether the image on disk predates it)"
        message="📦 Karakos **${latest_tag}** is out. This install is not pinned — it tracks \`latest\` — so it is not stuck on an older release, but the image on disk only moves when it is pulled: \`docker compose pull && docker compose up -d\`. Release notes: ${release_url}"
    else
        log "release available: $latest_tag (running tag \`$current\` is not a release version, so it cannot be compared)"
        message="📦 Karakos **${latest_tag}** is out. This install runs the image tag \`${current}\`, which is not a release version, so I cannot tell whether it is behind. To move to the release: set \`KARAKOS_VERSION=${latest_tag}\` in \`config/.env\`, then \`docker compose pull && docker compose up -d\`. Release notes: ${release_url}"
    fi

    # Record only after the notice actually went out, so a failed send is
    # retried next Monday instead of being marked delivered.
    notify "$message" || return 1
    write_last_announced "$latest_tag"
    return 0
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force) FORCE=true; shift ;;
            --channel) ALERT_CHANNEL="${2:-}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
        esac
    done
    run_check
}

main "$@"
