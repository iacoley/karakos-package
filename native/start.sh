#!/usr/bin/env bash
# native/start.sh — one-time bootstrap for a native (no-Docker) Karakos
# install, run before the systemd units are first started. Replaces the
# per-container-start logic in bin/entrypoint.sh; the parts that were about
# *starting supervisord* are gone (systemd owns that now), the parts about
# *preparing the workspace* are kept, unchanged in spirit.
#
# Usage: WORKSPACE_ROOT=/path/to/install bash native/start.sh
#
# Deliberately NOT run on every unit start (unlike entrypoint.sh, which ran
# on every container start) — these are one-time/idempotent setup steps,
# not startup checks. Re-run manually after a config change if needed; each
# step is safe to repeat.

set -euo pipefail

export WORKSPACE_ROOT="${WORKSPACE_ROOT:?WORKSPACE_ROOT must be set}"
cd "$WORKSPACE_ROOT"

echo "==> Karakos native bootstrap: $WORKSPACE_ROOT"

# --- Required env vars (same check as entrypoint.sh) ---
required_vars=("DASHBOARD_PORT" "AGENT_SERVER_TOKEN")
missing_vars=()
for var in "${required_vars[@]}"; do
    if [ -z "${!var:-}" ]; then
        missing_vars+=("$var")
    fi
done
if [ ${#missing_vars[@]} -gt 0 ]; then
    echo "ERROR: Required environment variables not set: ${missing_vars[*]}" >&2
    echo "(these normally come from config/.env, sourced via each unit's EnvironmentFile=)" >&2
    exit 1
fi

# --- No volume-writability check ---
# That check existed for a Docker-specific failure mode: a named volume
# whose ownership was seeded from an older image and left root-owned.
# Native has no named volumes — it's just the filesystem under whatever
# user runs this script and the systemd units (User=@@KARAKOS_USER@@). Not
# needed; not ported.

# --- Data/log/inbox directories ---
mkdir -p \
    "$WORKSPACE_ROOT/data/messages" \
    "$WORKSPACE_ROOT/data/memory" \
    "$WORKSPACE_ROOT/data/health" \
    "$WORKSPACE_ROOT/logs/agent-streams" \
    "$WORKSPACE_ROOT/logs/session-summaries" \
    "$WORKSPACE_ROOT/inbox"

# --- Per-agent inbox/journal directories ---
if [ -f "$WORKSPACE_ROOT/config/agents.json" ]; then
    for agent in $(python3 -c "import json; print(' '.join(json.load(open('$WORKSPACE_ROOT/config/agents.json'))['agents'].keys()))"); do
        mkdir -p "$WORKSPACE_ROOT/inbox/$agent"
        mkdir -p "$WORKSPACE_ROOT/agents/$agent/inbox"
        mkdir -p "$WORKSPACE_ROOT/agents/$agent/journal"
    done
fi

# --- git init (used by the protected-paths pre-commit hook) ---
if [ ! -d "$WORKSPACE_ROOT/.git" ]; then
    git -c init.defaultBranch=main init -q
    git -c user.email=karakos@local -c user.name=karakos \
        commit --allow-empty -q -m "Initial commit"
fi

# --- Install protected-paths pre-commit hook ---
if [ -f "$WORKSPACE_ROOT/system/check-protected-paths.py" ]; then
    cp "$WORKSPACE_ROOT/system/install-hooks.sh" "$WORKSPACE_ROOT/.git/hooks/pre-commit" 2>/dev/null || true
    chmod +x "$WORKSPACE_ROOT/.git/hooks/pre-commit" 2>/dev/null || true
fi

# --- Install auto-reload-on-commit post-commit hook ---
# STILL OPEN as far as *this repo* goes (2026-08-10 note below still
# applies here) — but the design question itself is answered now: see
# native/README.md's "Design fixed 2026-08-11, not yet in this codebase"
# note. Short version: bin/relay.py grew real graceful-drain-on-SIGTERM
# handling and reload-on-commit.py's bounce went async, verified live —
# just not in this repo yet, deliberately (this PR stays scoped to
# native/ only, to avoid a third set of edits to bin/relay.py landing on
# top of #136's). Port that fix into bin/relay.py /
# system/reload-on-commit.py here before treating this hook as safe to
# rely on natively. Original open-question note, unchanged below:
#
# Amos's shop does not run this pattern at all natively — two
# self-inflicted outages taught them that restarting the process
# delivering a reply drops it, and a bad native hot-patch has no image to
# fall back to the way a container did. Our version already excludes
# agent-server.py (the reply-generating process) for the same reason.
# Installed here for parity with the container in the meantime, not
# because the underlying mechanism is fixed in this codebase yet.
if [ -f "$WORKSPACE_ROOT/system/reload-on-commit.py" ]; then
    cp "$WORKSPACE_ROOT/system/install-post-commit-hook.sh" "$WORKSPACE_ROOT/.git/hooks/post-commit" 2>/dev/null || true
    chmod +x "$WORKSPACE_ROOT/.git/hooks/post-commit" 2>/dev/null || true
fi

# NOTE: system/reload-on-commit.py currently shells out to
# bin/safe-pkill.sh, which finds processes by pattern-matching the running
# command line — that still works unchanged against native systemd-managed
# processes (they're just python3 processes either way), but it means a
# reload bounces the process directly rather than going through
# `systemctl restart`. Fine for now (systemd's Restart=always just respawns
# it), but if we want journal/status to reflect restarts cleanly, this
# should move to `systemctl restart karakos-<unit>` instead of a raw
# SIGTERM. Not changed yet — flagging for the actual build, not fixing
# silently in a bootstrap script.

# --- Discord slash-command registration ---
if [ -n "${DISCORD_BOT_TOKEN_PRIMARY:-}" ] && [ -n "${DISCORD_BOT_ID_PRIMARY:-}" ] && [ -n "${DISCORD_SERVER_ID:-}" ]; then
    python3 "$WORKSPACE_ROOT/bin/register-discord-commands.py" || \
        echo "WARNING: Discord slash-command registration failed (see above). Continuing." >&2
fi

echo "==> Bootstrap complete. Next steps:"
echo "    1. Fill in @@INSTALL_DIR@@ / @@KARAKOS_USER@@ / @@DASHBOARD_PORT@@ placeholders"
echo "       in native/systemd/*.service (or generate them — not yet scripted)."
echo "    2. sudo cp native/systemd/*.service /etc/systemd/system/"
echo "    3. sudo systemctl daemon-reload"
echo "    4. sudo systemctl enable --now karakos-agent-server karakos-relay \\"
echo "         karakos-scheduler karakos-recovery-agent karakos-dashboard"
